"""
Unit tests for capacity checking and OCI error classification.
"""

import unittest
from unittest.mock import MagicMock

import oci

from src.capacity_checker import check_compute_capacity_report
from src.oci_client import ErrorCategory, classify_oci_error


class TestCapacityChecker(unittest.TestCase):
    """Test suite for capacity reporting and error categorization."""

    def test_capacity_report_available(self):
        """When capacity report indicates AVAILABLE, returns (True, ...)."""
        mock_compute = MagicMock()
        mock_item = MagicMock()
        mock_item.availability_status = "AVAILABLE"
        mock_compute.create_compute_capacity_report.return_value.data.shape_availabilities = [mock_item]

        is_avail, msg = check_compute_capacity_report(
            compute_client=mock_compute,
            compartment_id="ocid1.compartment.oc1..123",
            availability_domain="AD-1",
            shape="VM.Standard.A1.Flex",
            ocpus=2.0,
            memory_in_gbs=12.0,
        )

        self.assertTrue(is_avail)
        self.assertIn("AVAILABLE", msg)

    def test_capacity_report_out_of_capacity(self):
        """When capacity report indicates OUT_OF_HOST_CAPACITY, returns (False, ...)."""
        mock_compute = MagicMock()
        mock_item = MagicMock()
        mock_item.availability_status = "OUT_OF_HOST_CAPACITY"
        mock_compute.create_compute_capacity_report.return_value.data.shape_availabilities = [mock_item]

        is_avail, msg = check_compute_capacity_report(
            compute_client=mock_compute,
            compartment_id="ocid1.compartment.oc1..123",
            availability_domain="AD-1",
            shape="VM.Standard.A1.Flex",
            ocpus=2.0,
            memory_in_gbs=12.0,
        )

        self.assertFalse(is_avail)
        self.assertIn("OUT_OF_HOST_CAPACITY", msg)

    def test_capacity_report_service_error_falls_back(self):
        """If capacity report API is not supported on user tier, it falls back to authoritative launch."""
        mock_compute = MagicMock()
        mock_compute.create_compute_capacity_report.side_effect = oci.exceptions.ServiceError(
            status=404,
            code="NotSupportedOrNotFound",
            message="Capacity reporting is not enabled for this tenant",
            headers={},
        )

        is_avail, msg = check_compute_capacity_report(
            compute_client=mock_compute,
            compartment_id="ocid1.compartment.oc1..123",
            availability_domain="AD-1",
            shape="VM.Standard.A1.Flex",
            ocpus=2.0,
            memory_in_gbs=12.0,
        )

        self.assertTrue(is_avail)
        self.assertIn("authoritative launch", msg)

    def test_classify_out_of_capacity_errors(self):
        """All variations of out-of-capacity error messages classify as CAPACITY."""
        err1 = oci.exceptions.ServiceError(
            status=500,
            code="InternalError",
            message="Out of capacity for shape VM.Standard.A1.Flex in availability domain AD-1.",
            headers={},
        )
        cat1, _ = classify_oci_error(err1)
        self.assertEqual(cat1, ErrorCategory.CAPACITY)

        err2 = oci.exceptions.ServiceError(
            status=500,
            code="OutOfCapacity",
            message="Host capacity unavailable",
            headers={},
        )
        cat2, _ = classify_oci_error(err2)
        self.assertEqual(cat2, ErrorCategory.CAPACITY)

        err3 = oci.exceptions.ServiceError(
            status=429,
            code="LimitExceeded",
            message="Insufficient host capacity to satisfy request",
            headers={},
        )
        cat3, _ = classify_oci_error(err3)
        self.assertEqual(cat3, ErrorCategory.CAPACITY)

    def test_classify_auth_errors(self):
        """Authentication and authorization failures classify as fatal AUTHENTICATION."""
        err_401 = oci.exceptions.ServiceError(
            status=401,
            code="NotAuthenticated",
            message="The required information to complete authentication was not provided or was incorrect.",
            headers={},
        )
        cat_401, _ = classify_oci_error(err_401)
        self.assertEqual(cat_401, ErrorCategory.AUTHENTICATION)

        err_403 = oci.exceptions.ServiceError(
            status=403,
            code="NotAuthorizedOrNotFound",
            message="Authorization failed or requested resource not found.",
            headers={},
        )
        cat_403, _ = classify_oci_error(err_403)
        self.assertEqual(cat_403, ErrorCategory.AUTHENTICATION)

    def test_classify_config_errors(self):
        """Configuration errors like missing subnet or quota limits classify as fatal CONFIGURATION."""
        err_quota = oci.exceptions.ServiceError(
            status=400,
            code="QuotaExceeded",
            message="You have exceeded your service limit for A1 Flex cores.",
            headers={},
        )
        cat_quota, _ = classify_oci_error(err_quota)
        self.assertEqual(cat_quota, ErrorCategory.CONFIGURATION)

        err_404 = oci.exceptions.ServiceError(
            status=404,
            code="SubnetNotFound",
            message="Subnet ocid1.subnet.oc1... does not exist.",
            headers={},
        )
        cat_404, _ = classify_oci_error(err_404)
        self.assertEqual(cat_404, ErrorCategory.CONFIGURATION)


if __name__ == "__main__":
    unittest.main()
