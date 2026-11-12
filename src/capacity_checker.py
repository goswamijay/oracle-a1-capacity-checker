"""
Capacity checking module using OCI Compute Capacity Reporting API.
Provides an advisory check prior to launch, while treating the actual
instance launch response as authoritative.
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

import oci

from src.oci_client import ErrorCategory, classify_oci_error

logger = logging.getLogger("oci_checker.capacity_checker")


def check_compute_capacity_report(
    compute_client: oci.core.ComputeClient,
    compartment_id: str,
    availability_domain: str,
    shape: str,
    ocpus: float,
    memory_in_gbs: float,
) -> Tuple[bool, str]:
    """Query OCI's CreateComputeCapacityReport API to inspect available host capacity.
    
    Returns:
        Tuple of:
          - is_likely_available (bool): True if available or check is inconclusive; False if confirmed out of capacity.
          - summary_message (str): Descriptive status message.
    """
    logger.info(
        "Querying OCI Compute Capacity Report for shape %s (%.1f OCPUs, %.1f GB) in %s...",
        shape,
        ocpus,
        memory_in_gbs,
        availability_domain,
    )

    try:
        shape_config = oci.core.models.CapacityReportInstanceShapeConfig(
            ocpus=float(ocpus),
            memory_in_gbs=float(memory_in_gbs),
        )

        shape_availability = oci.core.models.CreateCapacityReportShapeAvailabilityDetails(
            instance_shape=shape,
            instance_shape_config=shape_config,
        )

        report_details = oci.core.models.CreateComputeCapacityReportDetails(
            compartment_id=compartment_id,
            availability_domain=availability_domain,
            shape_availabilities=[shape_availability],
        )

        response = compute_client.create_compute_capacity_report(
            create_compute_capacity_report_details=report_details
        )

        if not response.data or not response.data.shape_availabilities:
            msg = "Empty capacity report returned by OCI. Proceeding to authoritative launch attempt."
            logger.warning(msg)
            return True, msg

        item = response.data.shape_availabilities[0]
        status = getattr(item, "availability_status", "UNKNOWN")
        logger.info("Capacity Report Result: availability_status = %s", status)

        if status == "AVAILABLE":
            return True, "Capacity report status: AVAILABLE"
        elif status == "OUT_OF_HOST_CAPACITY":
            return False, "Capacity report status: OUT_OF_HOST_CAPACITY"
        else:
            # Other statuses (e.g. HARDWARE_NOT_SUPPORTED, etc.)
            return False, f"Capacity report status: {status}"

    except oci.exceptions.ServiceError as exc:
        category, explanation = classify_oci_error(exc)
        logger.info(
            "Capacity Report API returned (%s - %s): %s. "
            "Note: Capacity report is advisory; launch attempt will serve as authoritative test.",
            exc.status,
            exc.code,
            explanation,
        )
        if category == ErrorCategory.AUTHENTICATION:
            # Surface authentication errors immediately
            raise
        # For non-auth errors on capacity report (e.g., if report API is restricted on Free Tier),
        # return True with note so launch attempt can proceed authoritatively
        return True, f"Capacity report check skipped ({exc.code}): proceeding to authoritative launch"
    except Exception as exc:
        logger.warning(
            "Unexpected error during capacity report check: %s. Proceeding to launch attempt.",
            str(exc),
        )
        return True, f"Advisory check exception: {str(exc)}"
