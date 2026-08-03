"""Tests for the emergency cleanup sweeps.

These exist because the sweep is the last line of defence against a leaked GPU
instance, and it was previously incomplete in two ways that a live run proved
real: it never looked at capacity reservations (the most expensive resource,
~$114/hr for a leaked p6 ODCR) or launch templates, and it only ever covered one
region at a time even though experiments fall back across four.

Running the completed sweep against the real account found **16 orphaned launch
templates** — 8 in us-west-2 and 8 in us-east-2 — that every prior check missed.

boto3 is mocked directly rather than via moto, matching
``test_capacity_strategies.py``: moto's EC2 Fleet / Capacity Reservation coverage
is incomplete.
"""
from __future__ import annotations

import pytest
from botocore.exceptions import ClientError

from vllm_ec2_bench import cleanup

PROJECT = "medgemma-27b-benchmark"


class FakeEc2:
    """Minimal EC2 stub recording the destructive calls it receives."""

    def __init__(
        self,
        instances: list[str] | None = None,
        reservations: list[dict] | None = None,
        launch_templates: list[str] | None = None,
        security_groups: list[str] | None = None,
        fail_on: set[str] | None = None,
    ) -> None:
        self._instances = instances or []
        self._reservations = reservations or []
        self._launch_templates = launch_templates or []
        self._security_groups = security_groups or []
        self._fail_on = fail_on or set()
        self.terminated: list[str] = []
        self.cancelled: list[str] = []
        self.deleted_templates: list[str] = []
        self.deleted_sgs: list[str] = []

    def _maybe_fail(self, op: str) -> None:
        if op in self._fail_on:
            raise ClientError(
                {"Error": {"Code": "UnauthorizedOperation", "Message": "nope"}}, op
            )

    # -- describes ---------------------------------------------------------
    def describe_instances(self, **_kw):
        return {
            "Reservations": [
                {"Instances": [{"InstanceId": i} for i in self._instances]}
            ]
        }

    def describe_capacity_reservations(self, **_kw):
        self._maybe_fail("DescribeCapacityReservations")
        return {"CapacityReservations": self._reservations}

    def describe_launch_templates(self, **_kw):
        self._maybe_fail("DescribeLaunchTemplates")
        return {"LaunchTemplates": [{"LaunchTemplateId": t} for t in self._launch_templates]}

    def describe_security_groups(self, **_kw):
        return {"SecurityGroups": [{"GroupId": g} for g in self._security_groups]}

    # -- destructive -------------------------------------------------------
    # boto3 requires PascalCase keyword arguments, so the stub must match the
    # real API signature exactly; N803 is suppressed rather than renamed.
    def terminate_instances(self, InstanceIds):  # noqa: N803
        self.terminated.extend(InstanceIds)

    def cancel_capacity_reservation(self, CapacityReservationId):  # noqa: N803
        self._maybe_fail("CancelCapacityReservation")
        self.cancelled.append(CapacityReservationId)

    def delete_launch_template(self, LaunchTemplateId):  # noqa: N803
        self._maybe_fail("DeleteLaunchTemplate")
        self.deleted_templates.append(LaunchTemplateId)

    def delete_security_group(self, GroupId):  # noqa: N803
        self._maybe_fail("DeleteSecurityGroup")
        self.deleted_sgs.append(GroupId)


@pytest.fixture
def patch_boto(monkeypatch):
    """Route every boto3.client call to one shared stub."""
    holder: dict[str, FakeEc2] = {}

    def install(fake: FakeEc2) -> FakeEc2:
        holder["fake"] = fake
        monkeypatch.setattr(
            cleanup.boto3, "client", lambda *_a, **_k: holder["fake"]
        )
        return fake

    return install


class TestCancelCapacityReservations:
    def test_cancels_active_reservations(self, patch_boto) -> None:
        fake = patch_boto(
            FakeEc2(
                reservations=[
                    {
                        "CapacityReservationId": "cr-1",
                        "InstanceType": "p6-b200.48xlarge",
                        "TotalInstanceCount": 1,
                    }
                ]
            )
        )
        assert cleanup.cancel_tagged_capacity_reservations("us-west-2", PROJECT) == ["cr-1"]
        assert fake.cancelled == ["cr-1"]

    def test_nothing_to_cancel(self, patch_boto) -> None:
        patch_boto(FakeEc2())
        assert cleanup.cancel_tagged_capacity_reservations("us-west-2", PROJECT) == []

    def test_describe_failure_is_not_fatal(self, patch_boto) -> None:
        """A permissions gap must not abort the rest of the sweep."""
        patch_boto(FakeEc2(fail_on={"DescribeCapacityReservations"}))
        assert cleanup.cancel_tagged_capacity_reservations("us-west-2", PROJECT) == []

    def test_one_failed_cancel_does_not_stop_the_others(self, patch_boto) -> None:
        fake = patch_boto(
            FakeEc2(
                reservations=[{"CapacityReservationId": "cr-1"}],
                fail_on={"CancelCapacityReservation"},
            )
        )
        assert cleanup.cancel_tagged_capacity_reservations("us-west-2", PROJECT) == []
        assert fake.cancelled == []


class TestCleanupLaunchTemplates:
    def test_deletes_orphaned_templates(self, patch_boto) -> None:
        """The real leak: 16 orphans were found in the live account."""
        fake = patch_boto(FakeEc2(launch_templates=[f"lt-{i}" for i in range(8)]))
        deleted = cleanup.cleanup_tagged_launch_templates("us-west-2", PROJECT)
        assert len(deleted) == 8
        assert fake.deleted_templates == deleted

    def test_describe_failure_is_not_fatal(self, patch_boto) -> None:
        patch_boto(FakeEc2(fail_on={"DescribeLaunchTemplates"}))
        assert cleanup.cleanup_tagged_launch_templates("us-west-2", PROJECT) == []


class TestSweepAll:
    def test_covers_every_region_and_resource_kind(self, patch_boto) -> None:
        fake = patch_boto(
            FakeEc2(
                instances=["i-1"],
                reservations=[{"CapacityReservationId": "cr-1"}],
                launch_templates=["lt-1"],
                security_groups=["sg-1"],
            )
        )
        regions = ["us-west-2", "us-east-1", "us-east-2", "ap-south-1"]
        report = cleanup.sweep_all(PROJECT, regions)

        assert list(report) == regions, "every region must be swept"
        for region in regions:
            assert set(report[region]) == {
                "instances",
                "capacity_reservations",
                "launch_templates",
                "security_groups",
            }
        # The stub answers for all four regions, so each kind is hit four times.
        assert len(fake.terminated) == 4
        assert len(fake.cancelled) == 4
        assert len(fake.deleted_templates) == 4
        assert len(fake.deleted_sgs) == 4

    def test_clean_account_reports_all_empty(self, patch_boto) -> None:
        patch_boto(FakeEc2())
        report = cleanup.sweep_all(PROJECT, ["us-west-2"])
        assert report["us-west-2"] == {
            "instances": [],
            "capacity_reservations": [],
            "launch_templates": [],
            "security_groups": [],
        }
        total = sum(len(v) for v in report["us-west-2"].values())
        assert total == 0, "a clean account must sum to zero for an assertable sweep"

    def test_instances_swept_before_security_groups(self, patch_boto) -> None:
        """Ordering matters: an SG cannot be deleted while an ENI holds it."""
        calls: list[str] = []

        class Ordered(FakeEc2):
            def terminate_instances(self, InstanceIds):  # noqa: N803
                calls.append("instances")
                super().terminate_instances(InstanceIds)

            def delete_security_group(self, GroupId):  # noqa: N803
                calls.append("sgs")
                super().delete_security_group(GroupId)

        patch_boto(Ordered(instances=["i-1"], security_groups=["sg-1"]))
        cleanup.sweep_all(PROJECT, ["us-west-2"])
        assert calls.index("instances") < calls.index("sgs")
