"""Emergency / bulk cleanup helpers.

These are project-wide (not per-experiment) — intended for the "oh no, my
kernel crashed and I don't know what's left running" case.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence

import boto3
from botocore.exceptions import ClientError

LOG = logging.getLogger(__name__)

_PROJECT_TAG_KEY = "Project"


def terminate_all_tagged_instances(
    region: str,
    project_tag_value: str,
) -> list[str]:
    """Terminate every non-terminated instance tagged ``Project=project_tag_value``.

    Returns the list of instance IDs terminated (may be empty).
    """
    ec2 = boto3.client("ec2", region_name=region)
    resp = ec2.describe_instances(
        Filters=[
            {"Name": f"tag:{_PROJECT_TAG_KEY}", "Values": [project_tag_value]},
            {
                "Name": "instance-state-name",
                "Values": ["pending", "running", "stopping", "stopped"],
            },
        ],
    )
    ids: list[str] = []
    for reservation in resp.get("Reservations", []):
        for inst in reservation.get("Instances", []):
            ids.append(inst["InstanceId"])
    if ids:
        LOG.info("Emergency sweep terminating: %s", ids)
        ec2.terminate_instances(InstanceIds=ids)
    return ids


def cleanup_tagged_security_groups(
    region: str,
    project_tag_value: str,
) -> list[str]:
    """Delete every SG tagged ``Project=project_tag_value`` that isn't in use.

    Returns the list of SG IDs deleted. Skips (with a warning) any SG that
    still has a dependency (ENI attached, etc.).
    """
    ec2 = boto3.client("ec2", region_name=region)
    resp = ec2.describe_security_groups(
        Filters=[{"Name": f"tag:{_PROJECT_TAG_KEY}", "Values": [project_tag_value]}]
    )
    deleted: list[str] = []
    for sg in resp.get("SecurityGroups", []):
        sg_id = sg["GroupId"]
        try:
            ec2.delete_security_group(GroupId=sg_id)
            deleted.append(sg_id)
        except ClientError as exc:
            LOG.warning("Skipping SG %s (%s): %s", sg_id, sg.get("GroupName"), exc)
    return deleted


def cancel_tagged_capacity_reservations(
    region: str,
    project_tag_value: str,
) -> list[str]:
    """Cancel every active capacity reservation tagged for this project.

    **This is the expensive one.** An auto-created ODCR keeps billing at the
    on-demand rate whether or not an instance occupies it, so a leaked
    reservation for a p6-b200.48xlarge costs ~$114/hr indefinitely. Instances
    and security groups were already swept here; reservations were not, which
    left the highest-cost resource as the only unmonitored one.
    """
    ec2 = boto3.client("ec2", region_name=region)
    try:
        resp = ec2.describe_capacity_reservations(
            Filters=[
                {"Name": f"tag:{_PROJECT_TAG_KEY}", "Values": [project_tag_value]},
                {"Name": "state", "Values": ["active", "pending"]},
            ],
        )
    except ClientError as exc:
        LOG.warning("Could not list capacity reservations in %s: %s", region, exc)
        return []
    cancelled: list[str] = []
    for cr in resp.get("CapacityReservations", []):
        cr_id = cr["CapacityReservationId"]
        try:
            ec2.cancel_capacity_reservation(CapacityReservationId=cr_id)
            cancelled.append(cr_id)
            LOG.info(
                "Cancelled capacity reservation %s (%s x%s)",
                cr_id, cr.get("InstanceType"), cr.get("TotalInstanceCount"),
            )
        except ClientError as exc:
            LOG.warning("Could not cancel reservation %s: %s", cr_id, exc)
    return cancelled


def cleanup_tagged_launch_templates(
    region: str,
    project_tag_value: str,
) -> list[str]:
    """Delete every launch template tagged for this project.

    Launch templates are free, but they count against a per-region quota, and an
    unexpected exception during capacity acquisition leaks one per attempt — a
    persistent spot wait can make several. Nothing else swept them, so they
    accumulated silently until the quota bit.
    """
    ec2 = boto3.client("ec2", region_name=region)
    try:
        resp = ec2.describe_launch_templates(
            Filters=[{"Name": f"tag:{_PROJECT_TAG_KEY}", "Values": [project_tag_value]}],
        )
    except ClientError as exc:
        LOG.warning("Could not list launch templates in %s: %s", region, exc)
        return []
    deleted: list[str] = []
    for lt in resp.get("LaunchTemplates", []):
        lt_id = lt["LaunchTemplateId"]
        try:
            ec2.delete_launch_template(LaunchTemplateId=lt_id)
            deleted.append(lt_id)
        except ClientError as exc:
            LOG.warning("Could not delete launch template %s: %s", lt_id, exc)
    return deleted


def sweep_all(
    project_tag_value: str,
    regions: Sequence[str],
) -> dict[str, dict[str, list[str]]]:
    """Run every sweep across every region the harness can launch in.

    The individual helpers each take a single region, which meant a leak in a
    fallback region stayed invisible unless someone remembered to re-run the
    sweep by hand for that region. Experiments here default to us-west-2 but fall
    back to us-east-1/us-east-2/ap-south-1 for scarce accelerators, so a
    one-region sweep is not a safety net.

    Ordered so nothing blocks: instances first (frees the ENIs holding security
    groups), then reservations, then the free-but-quota-bound resources.

    Returns ``{region: {resource_kind: [ids]}}``, empty lists where nothing was
    found, so the caller can assert the sweep actually came back clean.
    """
    report: dict[str, dict[str, list[str]]] = {}
    for region in regions:
        report[region] = {
            "instances": terminate_all_tagged_instances(region, project_tag_value),
            "capacity_reservations": cancel_tagged_capacity_reservations(
                region, project_tag_value
            ),
            "launch_templates": cleanup_tagged_launch_templates(
                region, project_tag_value
            ),
            "security_groups": cleanup_tagged_security_groups(
                region, project_tag_value
            ),
        }
        found = {k: v for k, v in report[region].items() if v}
        if found:
            LOG.warning("Sweep in %s removed: %s", region, found)
    return report


__all__ = [
    "cancel_tagged_capacity_reservations",
    "cleanup_tagged_launch_templates",
    "cleanup_tagged_security_groups",
    "sweep_all",
    "terminate_all_tagged_instances",
]
