"""Teardown must survive transient failures, because the money is in step 4.

An auto-created capacity reservation bills the full on-demand rate whether or not
an instance occupies it — up to ~$114/hr for a p6-b200. Two defects meant a single
network blip could leave one running:

* ``terminate()`` guarded each step with ``except ClientError`` only, so a
  ``BotoCoreError`` on step 1 propagated and skipped steps 2-5, including the
  reservation cancel.
* ``ODCRStrategy.launch()`` created the reservation, then made an unguarded
  ``describe_subnets`` call before its ``try``, and its handler also caught
  ``ClientError`` only.

``BotoCoreError`` subclasses like ``ReadTimeoutError`` are NOT ``ClientError``,
which is what made both holes reachable in normal operation.
"""
from __future__ import annotations

import types

from botocore.exceptions import ReadTimeoutError

import vllm_ec2_bench.deployer.capacity.odcr as odcr_mod
from vllm_ec2_bench.deployer.capacity.odcr import ODCRStrategy
from vllm_ec2_bench.deployer.runner import DeploymentRunner


class TestTerminateResilience:
    @staticmethod
    def _runner(seen: list[str], *, fleet_unsuccessful: bool = False):
        class Ec2:
            def terminate_instances(self, **_kw):
                seen.append("terminate")
                # A read timeout, not a ClientError — the case that used to
                # abort teardown before the reservation was cancelled.
                raise ReadTimeoutError(endpoint_url="https://ec2.example")

            def delete_fleets(self, **_kw):
                seen.append("fleet")
                if fleet_unsuccessful:
                    return {
                        "UnsuccessfulFleetDeletions": [
                            {"FleetId": "fleet-1", "Error": {"Message": "nope"}}
                        ]
                    }
                return {}

            def delete_launch_template(self, **_kw):
                seen.append("lt")

            def cancel_capacity_reservation(self, **_kw):
                seen.append("odcr")

        class State(types.SimpleNamespace):
            def mark_terminated(self):
                seen.append("marked")

        runner = DeploymentRunner.__new__(DeploymentRunner)
        runner.ec2 = Ec2()
        runner._resources = types.SimpleNamespace(
            teardown=lambda: seen.append("sg")
        )
        runner.config = types.SimpleNamespace(
            deployment=types.SimpleNamespace(experiment_id="exp_test")
        )
        runner.state = State(
            instance_id="i-1",
            spot_fleet_id="fleet-1",
            launch_template_id="lt-1",
            auto_created_odcr_id="cr-1",
            capacity_mode="odcr",
        )
        return runner

    def test_botocore_error_on_step1_does_not_skip_odcr_cancel(self) -> None:
        seen: list[str] = []
        self._runner(seen).terminate()
        assert "odcr" in seen, "the billing-critical step must still run"
        assert "sg" in seen
        assert seen.index("terminate") < seen.index("odcr")

    def test_all_five_steps_attempted(self) -> None:
        seen: list[str] = []
        self._runner(seen).terminate()
        for step in ("terminate", "fleet", "lt", "odcr", "sg"):
            assert step in seen, f"step {step} was skipped"

    def test_unsuccessful_fleet_deletion_is_surfaced(self, caplog) -> None:
        """delete_fleets reports failures in the body with HTTP 200, not a raise."""
        seen: list[str] = []
        with caplog.at_level("WARNING"):
            self._runner(seen, fleet_unsuccessful=True).terminate()
        assert any("not deleted" in r.getMessage() for r in caplog.records), (
            "a surviving fleet must be logged, not swallowed"
        )

    def test_state_cleared_so_second_call_is_a_noop(self) -> None:
        seen: list[str] = []
        runner = self._runner(seen)
        runner.terminate()
        first = len(seen)
        runner.terminate()
        # Only mark_terminated and the unconditional SG teardown repeat.
        assert seen[first:] in ([], ["sg", "marked"]), seen[first:]


class TestOdcrLeakPaths:
    @staticmethod
    def _ctx(fail: str | None, cancelled: list[str]):
        class Ec2:
            def describe_subnets(self, **_kw):
                if fail == "subnets":
                    raise ReadTimeoutError(endpoint_url="https://ec2.example")
                return {
                    "Subnets": [
                        {"SubnetId": "subnet-1", "AvailabilityZone": "us-west-2a"}
                    ]
                }

            def run_instances(self, **_kw):
                if fail == "run_boto":
                    raise ReadTimeoutError(endpoint_url="https://ec2.example")
                if fail == "run_empty":
                    return {"Instances": []}
                return {"Instances": [{"InstanceId": "i-1"}]}

            def cancel_capacity_reservation(self, CapacityReservationId):  # noqa: N803
                cancelled.append(CapacityReservationId)

        class Ctx:
            def __init__(self) -> None:
                self.ec2 = Ec2()
                self.config = types.SimpleNamespace(
                    deployment=types.SimpleNamespace(experiment_id="exp_test")
                )

            def get_subnets_for_preferred_azs(self):
                resp = self.ec2.describe_subnets()
                return {
                    s["AvailabilityZone"]: s["SubnetId"] for s in resp["Subnets"]
                }

        return Ctx()

    @staticmethod
    def _strategy(monkeypatch):
        strategy = ODCRStrategy()
        monkeypatch.setattr(
            strategy, "_auto_create_odcr", lambda _ctx: ("cr-X", "us-west-2a")
        )
        monkeypatch.setattr(
            odcr_mod, "_build_run_instances_params", lambda _ctx: {"ImageId": "ami-1"}
        )
        return strategy

    def test_subnet_lookup_timeout_cancels_the_reservation(self, monkeypatch) -> None:
        """describe_subnets used to sit OUTSIDE the try — a throttle leaked $114/hr."""
        cancelled: list[str] = []
        strategy = self._strategy(monkeypatch)
        try:
            strategy.launch(self._ctx("subnets", cancelled))
        except ReadTimeoutError:
            pass
        else:  # pragma: no cover
            raise AssertionError("expected the timeout to propagate")
        assert cancelled == ["cr-X"]

    def test_run_instances_timeout_cancels_the_reservation(self, monkeypatch) -> None:
        cancelled: list[str] = []
        strategy = self._strategy(monkeypatch)
        try:
            strategy.launch(self._ctx("run_boto", cancelled))
        except ReadTimeoutError:
            pass
        else:  # pragma: no cover
            raise AssertionError("expected the timeout to propagate")
        assert cancelled == ["cr-X"]

    def test_empty_instances_list_cancels_the_reservation(self, monkeypatch) -> None:
        """An empty Instances list used to raise IndexError past the handler."""
        cancelled: list[str] = []
        strategy = self._strategy(monkeypatch)
        try:
            strategy.launch(self._ctx("run_empty", cancelled))
        except RuntimeError:
            pass
        else:  # pragma: no cover
            raise AssertionError("expected a RuntimeError")
        assert cancelled == ["cr-X"]

    def test_success_keeps_the_reservation(self, monkeypatch) -> None:
        cancelled: list[str] = []
        strategy = self._strategy(monkeypatch)
        result = strategy.launch(self._ctx(None, cancelled))
        assert result.instance_id == "i-1"
        assert result.auto_created_odcr_id == "cr-X"
        assert cancelled == [], "a successful launch must keep its reservation"
