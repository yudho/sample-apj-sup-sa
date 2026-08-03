"""ODCR strategy — auto-create a targeted On-Demand Capacity Reservation.

Useful when the regional OD pool is saturated but a specific AZ still has
headroom. Creating a CR "reserves" that capacity so :py:meth:`RunInstances`
against the CR id is guaranteed to succeed (until the CR is cancelled).

ODCRs accrue on-demand billing from the moment they become active, so the
runner must cancel auto-created ODCRs on teardown.
"""
from __future__ import annotations

import logging

from botocore.exceptions import ClientError

from .base import CapacityExhausted, CapacityStrategy, LaunchContext, LaunchResult
from .ondemand import _build_run_instances_params

LOG = logging.getLogger(__name__)


class ODCRStrategy(CapacityStrategy):
    """Auto-create a targeted ODCR, then launch against it."""

    mode = "odcr"

    def launch(self, ctx: LaunchContext) -> LaunchResult:
        cfg = ctx.config.deployment
        odcr_id, odcr_az = self._auto_create_odcr(ctx)

        # EVERYTHING after the reservation exists must be guarded. An ODCR bills
        # the full on-demand rate whether or not an instance occupies it — up to
        # ~$114/hr for a p6-b200 — so any escape between creation here and a
        # successful launch leaks money indefinitely.
        #
        # Two holes this closes:
        #  * get_subnets_for_preferred_azs() makes a describe_subnets call and
        #    used to sit OUTSIDE the try, so a throttle there leaked the ODCR.
        #  * the handler caught ClientError only, so a BotoCoreError
        #    (ReadTimeout, EndpointConnectionError) or an IndexError from an
        #    empty Instances list escaped uncancelled.
        try:
            run_params = _build_run_instances_params(ctx)
            run_params["CapacityReservationSpecification"] = {
                "CapacityReservationTarget": {"CapacityReservationId": odcr_id},
            }
            subnets_by_az = ctx.get_subnets_for_preferred_azs()
            if odcr_az in subnets_by_az:
                run_params["SubnetId"] = subnets_by_az[odcr_az]

            LOG.info(
                "[%s] RunInstances against auto-ODCR %s in %s",
                cfg.experiment_id, odcr_id, odcr_az,
            )
            resp = ctx.ec2.run_instances(**run_params)
            instances = resp.get("Instances") or []
            if not instances:
                raise RuntimeError(
                    f"[{cfg.experiment_id}] RunInstances against ODCR {odcr_id} "
                    "returned no instances"
                )
            instance = instances[0]
        except BaseException:
            # BaseException so a KeyboardInterrupt mid-launch cannot leave a
            # billing reservation behind either.
            LOG.warning(
                "[%s] launch failed after creating ODCR %s — cancelling it",
                cfg.experiment_id, odcr_id,
            )
            try:
                ctx.ec2.cancel_capacity_reservation(CapacityReservationId=odcr_id)
            except Exception as exc:  # noqa: BLE001
                LOG.error(
                    "[%s] COULD NOT CANCEL ODCR %s — it is still billing, cancel "
                    "it by hand: %s", cfg.experiment_id, odcr_id, exc,
                )
            raise

        return LaunchResult(
            instance_id=instance["InstanceId"],
            availability_zone=odcr_az,
            subnet_id=run_params.get("SubnetId", ""),
            capacity_mode=self.mode,
            auto_created_odcr_id=odcr_id,
        )

    # ------------------------------------------------------------------
    def _auto_create_odcr(self, ctx: LaunchContext) -> tuple[str, str]:
        cfg = ctx.config.deployment
        # Prefer explicit preferred_azs, else all offered AZs.
        subnets_by_az = ctx.get_subnets_for_preferred_azs()
        azs_to_try = list(cfg.preferred_azs) or list(subnets_by_az.keys())
        if not azs_to_try:
            raise CapacityExhausted(
                f"No AZs available to try ODCR creation for {cfg.instance_type}"
            )

        last_err: Exception | None = None
        for az in azs_to_try:
            try:
                LOG.info("[%s] Auto-creating ODCR in %s", cfg.experiment_id, az)
                resp = ctx.ec2.create_capacity_reservation(
                    InstanceType=cfg.instance_type,
                    InstancePlatform="Linux/UNIX",
                    AvailabilityZone=az,
                    Tenancy="default",
                    InstanceCount=1,
                    EndDateType="unlimited",
                    InstanceMatchCriteria="targeted",
                    TagSpecifications=[{
                        "ResourceType": "capacity-reservation",
                        "Tags": ctx.tags + [{"Key": "Purpose", "Value": "auto-odcr"}],
                    }],
                )
                cr = resp["CapacityReservation"]
                return cr["CapacityReservationId"], az
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                LOG.warning(
                    "[%s] ODCR creation in %s failed (%s): %s",
                    cfg.experiment_id, az, code, exc,
                )
                last_err = exc
                continue

        raise CapacityExhausted(
            f"Auto-ODCR failed in all AZs {azs_to_try}: {last_err}"
        ) from last_err


__all__ = ["ODCRStrategy"]
