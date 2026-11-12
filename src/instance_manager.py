"""
Instance lifecycle management, duplicate prevention, and boot volume safety checks.
Ensures zero data loss, safe preservation of existing boot volume, and atomic launch.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import oci

from src.config import Config
from src.oci_client import OCIClients

logger = logging.getLogger("oci_checker.instance_manager")

ACTIVE_INSTANCE_STATES = {
    "PROVISIONING",
    "STAGING",
    "STARTING",
    "RUNNING",
    "STOPPING",
    "STOPPED",
}


@dataclass
class InstanceInfo:
    """Summary of an existing or newly launched instance."""

    id: str
    display_name: str
    lifecycle_state: str
    shape: str
    availability_domain: str
    compartment_id: str
    time_created: Optional[str] = None


class SafetyCheckError(Exception):
    """Raised when an infrastructure prerequisite fails safety checks."""


def resolve_availability_domain(
    identity_client: oci.identity.IdentityClient,
    compartment_id: str,
    target_ad: str,
) -> str:
    """Resolve user-supplied AD (e.g. 'AD-1' or partial) to full OCI AD name.
    
    Example: 'AD-1' -> 'nBSt:AP-MUMBAI-1-AD-1'
    """
    logger.info("Resolving availability domain for input: '%s'", target_ad)
    try:
        response = identity_client.list_availability_domains(compartment_id=compartment_id)
        ads = response.data or []
    except Exception as exc:
        logger.warning(
            "Could not list availability domains via Identity API (%s). "
            "Assuming provided AD string is exact.",
            exc,
        )
        return target_ad

    ad_names = [ad.name for ad in ads]
    logger.info("Found %d availability domain(s) in tenancy: %s", len(ad_names), ad_names)

    # Exact match check
    for name in ad_names:
        if name.strip().lower() == target_ad.strip().lower():
            logger.info("Exact AD match found: %s", name)
            return name

    # Suffix / partial match check (e.g. 'AD-1' matches 'kONR:AP-MUMBAI-1-AD-1')
    target_clean = target_ad.strip().upper().replace(" ", "").replace("_", "-")
    for name in ad_names:
        name_clean = name.upper()
        if target_clean in name_clean or name_clean.endswith(f"-{target_clean}") or name_clean.endswith(f":{target_clean}"):
            logger.info("Matched AD '%s' to full OCI name '%s'", target_ad, name)
            return name

    # If only one AD exists in single-AD region like Mumbai (ap-mumbai-1)
    if len(ad_names) == 1 and ("1" in target_ad or "ad-1" in target_ad.lower()):
        logger.info("Single AD region detected; resolved to %s", ad_names[0])
        return ad_names[0]

    raise SafetyCheckError(
        f"Could not resolve target availability domain '{target_ad}'. "
        f"Available ADs in your compartment are: {ad_names}"
    )


def check_existing_instance(
    compute_client: oci.core.ComputeClient,
    compartment_id: str,
    instance_name: str,
) -> Optional[InstanceInfo]:
    """Check if an instance with target name already exists and is active.
    
    Returns InstanceInfo if active instance is found, None otherwise.
    """
    logger.info("Checking for existing instances named '%s' in compartment...", instance_name)
    try:
        response = compute_client.list_instances(
            compartment_id=compartment_id,
            display_name=instance_name,
        )
        instances = response.data or []
    except Exception as exc:
        logger.error("Failed to query instances in compartment: %s", exc)
        raise

    for inst in instances:
        state = inst.lifecycle_state.upper()
        if state in ACTIVE_INSTANCE_STATES:
            logger.warning(
                "Found existing active instance '%s' (OCID: %s, State: %s)",
                inst.display_name,
                inst.id,
                state,
            )
            return InstanceInfo(
                id=inst.id,
                display_name=inst.display_name,
                lifecycle_state=state,
                shape=inst.shape,
                availability_domain=inst.availability_domain,
                compartment_id=inst.compartment_id,
                time_created=str(getattr(inst, "time_created", "")),
            )

    logger.info("No active instance named '%s' exists. Safe to proceed.", instance_name)
    return None


def verify_boot_volume_safety(
    blockstorage_client: oci.core.BlockstorageClient,
    compute_client: oci.core.ComputeClient,
    compartment_id: str,
    boot_volume_id: str,
    target_ad: str,
) -> None:
    """Verify boot volume exists, is AVAILABLE, matches AD, and is NOT attached.
    
    Ensures zero data loss or conflict with active instances.
    """
    logger.info("Performing safety verification on boot volume: %s", boot_volume_id)

    # 1. Fetch boot volume metadata
    try:
        bv_response = blockstorage_client.get_boot_volume(boot_volume_id=boot_volume_id)
        boot_volume = bv_response.data
    except oci.exceptions.ServiceError as exc:
        raise SafetyCheckError(
            f"Failed to retrieve boot volume '{boot_volume_id}': {exc.message} "
            f"(Status: {exc.status}, Code: {exc.code})"
        ) from exc

    bv_state = boot_volume.lifecycle_state.upper()
    bv_ad = boot_volume.availability_domain
    bv_name = getattr(boot_volume, "display_name", "<unknown>")
    logger.info(
        "Boot volume '%s' details: State=%s, AD=%s, Size=%s GB",
        bv_name,
        bv_state,
        bv_ad,
        getattr(boot_volume, "size_in_gbs", "N/A"),
    )

    if bv_state != "AVAILABLE":
        raise SafetyCheckError(
            f"Boot volume '{bv_name}' ({boot_volume_id}) is in state '{bv_state}'. "
            f"It must be 'AVAILABLE' to launch a new instance."
        )

    # 2. Verify Availability Domain matches
    if bv_ad.strip().lower() != target_ad.strip().lower():
        raise SafetyCheckError(
            f"Boot volume Availability Domain mismatch! Boot volume is in '{bv_ad}', "
            f"but target instance launch AD is '{target_ad}'. OCI boot volumes can only boot instances "
            f"in the exact same Availability Domain."
        )

    # 3. Verify Boot Volume is NOT attached to another running instance
    logger.info("Checking active attachments for boot volume '%s'...", bv_name)
    try:
        att_response = compute_client.list_boot_volume_attachments(
            availability_domain=target_ad,
            compartment_id=compartment_id,
            boot_volume_id=boot_volume_id,
        )
        attachments = att_response.data or []
    except Exception as exc:
        raise SafetyCheckError(
            f"Failed to query boot volume attachments for '{boot_volume_id}': {exc}"
        ) from exc

    active_attachments = [
        att for att in attachments
        if att.lifecycle_state.upper() in ("ATTACHING", "ATTACHED")
    ]

    if active_attachments:
        att = active_attachments[0]
        raise SafetyCheckError(
            f"SAFETY ABORT: Boot volume '{bv_name}' is currently attached to instance OCID "
            f"'{att.instance_id}' (attachment state: '{att.lifecycle_state}'). "
            f"To preserve data integrity, this tool NEVER detaches volumes automatically. "
            f"Please detach the volume manually in OCI Console if you intend to reuse it."
        )

    logger.info("Boot volume safety verification passed. Volume is detached and ready.")


def verify_subnet_safety(
    virtual_network_client: oci.core.VirtualNetworkClient,
    subnet_id: str,
    target_ad: str,
) -> None:
    """Verify target subnet exists, is AVAILABLE, and compatible with target AD."""
    logger.info("Verifying subnet configuration for: %s", subnet_id)
    try:
        response = virtual_network_client.get_subnet(subnet_id=subnet_id)
        subnet = response.data
    except oci.exceptions.ServiceError as exc:
        raise SafetyCheckError(
            f"Failed to retrieve subnet '{subnet_id}': {exc.message} "
            f"(Status: {exc.status}, Code: {exc.code})"
        ) from exc

    subnet_state = subnet.lifecycle_state.upper()
    subnet_ad = getattr(subnet, "availability_domain", None)
    subnet_name = getattr(subnet, "display_name", "<unknown>")

    if subnet_state != "AVAILABLE":
        raise SafetyCheckError(
            f"Subnet '{subnet_name}' ({subnet_id}) is in state '{subnet_state}'. "
            f"Subnet must be 'AVAILABLE'."
        )

    # If subnet is AD-specific (subnet_ad is not None), verify it matches target_ad
    if subnet_ad:
        if subnet_ad.strip().lower() != target_ad.strip().lower():
            raise SafetyCheckError(
                f"Subnet '{subnet_name}' is AD-specific to '{subnet_ad}', "
                f"which conflicts with target instance launch AD '{target_ad}'."
            )
        logger.info("Subnet is AD-specific and correctly matches target AD: %s", subnet_ad)
    else:
        logger.info("Subnet is regional (valid across all Availability Domains in the region).")


def launch_instance_from_boot_volume(
    compute_client: oci.core.ComputeClient,
    config: Config,
    resolved_ad: str,
) -> InstanceInfo:
    """Execute authoritative instance launch from existing boot volume.
    
    Uses InstanceSourceViaBootVolumeDetails to preserve all existing data.
    """
    logger.info(
        "Initiating launch of instance '%s' [Shape: %s | OCPUs: %.1f | RAM: %.1f GB | AD: %s]...",
        config.instance_name,
        config.shape,
        config.ocpus,
        config.memory_in_gbs,
        resolved_ad,
    )

    source_details = oci.core.models.InstanceSourceViaBootVolumeDetails(
        source_type="bootVolume",
        boot_volume_id=config.boot_volume_id,
    )

    shape_config = oci.core.models.LaunchInstanceShapeConfigDetails(
        ocpus=float(config.ocpus),
        memory_in_gbs=float(config.memory_in_gbs),
    )

    create_vnic_details = oci.core.models.CreateVnicDetails(
        subnet_id=config.subnet_id,
        assign_public_ip=config.assign_public_ip,
        display_name=f"{config.instance_name}-vnic",
    )

    launch_details = oci.core.models.LaunchInstanceDetails(
        compartment_id=config.compartment_id,
        availability_domain=resolved_ad,
        display_name=config.instance_name,
        shape=config.shape,
        shape_config=shape_config,
        source_details=source_details,
        create_vnic_details=create_vnic_details,
        # fault_domain is deliberately omitted to let Oracle choose automatically
    )

    response = compute_client.launch_instance(launch_details)
    inst = response.data

    logger.info(
        "SUCCESS! Launch request accepted by OCI. Instance OCID: %s, State: %s",
        inst.id,
        inst.lifecycle_state,
    )

    return InstanceInfo(
        id=inst.id,
        display_name=inst.display_name,
        lifecycle_state=inst.lifecycle_state,
        shape=inst.shape,
        availability_domain=inst.availability_domain,
        compartment_id=inst.compartment_id,
        time_created=str(getattr(inst, "time_created", "")),
    )


def save_state(instance_info: InstanceInfo, filepath: str = "launch_state.json") -> None:
    """Persist launched instance details to disk."""
    try:
        path = Path(filepath)
        data = asdict(instance_info)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        logger.info("Saved instance launch state to %s", filepath)
    except Exception as exc:
        logger.warning("Could not persist launch state to %s: %s", filepath, exc)
