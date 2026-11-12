"""
Unit tests for duplicate prevention, boot volume safety checks, and instance launching.
"""

import unittest
from unittest.mock import MagicMock, patch

import oci

from src.config import Config
from src.instance_manager import (
    SafetyCheckError,
    check_existing_instance,
    launch_instance_from_boot_volume,
    resolve_availability_domain,
    verify_boot_volume_safety,
    verify_subnet_safety,
)


class TestInstanceManager(unittest.TestCase):
    """Test suite for instance management logic and safety checks."""

    def setUp(self):
        self.config = Config(
            tenancy="ocid1.tenancy.oc1..123",
            user="ocid1.user.oc1..123",
            fingerprint="aa:bb:cc:dd",
            region="ap-mumbai-1",
            key_content="dummy_key",
            compartment_id="ocid1.compartment.oc1..123",
            availability_domain="AD-1",
            subnet_id="ocid1.subnet.oc1.ap-mumbai-1..123",
            boot_volume_id="ocid1.bootvolume.oc1.ap-mumbai-1..123",
            instance_name="kron_new",
            shape="VM.Standard.A1.Flex",
            ocpus=2.0,
            memory_in_gbs=12.0,
            assign_public_ip=True,
        )

    def test_resolve_availability_domain_fuzzy_match(self):
        """Matches short name 'AD-1' against full tenancy AD string."""
        mock_identity = MagicMock()
        mock_ad = MagicMock()
        mock_ad.name = "kONR:AP-MUMBAI-1-AD-1"
        mock_identity.list_availability_domains.return_value.data = [mock_ad]

        resolved = resolve_availability_domain(
            mock_identity,
            compartment_id="ocid1.compartment.oc1..123",
            target_ad="AD-1",
        )
        self.assertEqual(resolved, "kONR:AP-MUMBAI-1-AD-1")

    def test_check_existing_instance_found(self):
        """Detects existing active instance and prevents duplicate launch."""
        mock_compute = MagicMock()
        inst1 = MagicMock()
        inst1.id = "ocid1.instance.oc1..already_running_vm"
        inst1.display_name = "kron_new"
        inst1.lifecycle_state = "RUNNING"
        inst1.shape = "VM.Standard.A1.Flex"
        inst1.availability_domain = "kONR:AP-MUMBAI-1-AD-1"
        inst1.compartment_id = "ocid1.compartment.oc1..123"

        mock_compute.list_instances.return_value.data = [inst1]

        res = check_existing_instance(mock_compute, "ocid1.compartment.oc1..123", "kron_new")
        self.assertIsNotNone(res)
        self.assertEqual(res.id, "ocid1.instance.oc1..already_running_vm")
        self.assertEqual(res.lifecycle_state, "RUNNING")

    def test_check_existing_instance_ignores_terminated(self):
        """Ignores TERMINATED instances and allows launch to proceed."""
        mock_compute = MagicMock()
        inst_term = MagicMock()
        inst_term.id = "ocid1.instance.oc1..old_dead_vm"
        inst_term.display_name = "kron_new"
        inst_term.lifecycle_state = "TERMINATED"

        mock_compute.list_instances.return_value.data = [inst_term]

        res = check_existing_instance(mock_compute, "ocid1.compartment.oc1..123", "kron_new")
        self.assertIsNone(res)

    def test_boot_volume_safety_fails_if_state_not_available(self):
        """Rejects boot volume if state is PROVISIONING or FAULTY."""
        mock_bs = MagicMock()
        mock_comp = MagicMock()

        bv = MagicMock()
        bv.lifecycle_state = "PROVISIONING"
        bv.availability_domain = "kONR:AP-MUMBAI-1-AD-1"
        bv.display_name = "kron-ai"
        mock_bs.get_boot_volume.return_value.data = bv

        with self.assertRaises(SafetyCheckError) as ctx:
            verify_boot_volume_safety(
                mock_bs, mock_comp, "comp_123", "bv_123", "kONR:AP-MUMBAI-1-AD-1"
            )
        self.assertIn("must be 'AVAILABLE'", str(ctx.exception))

    def test_boot_volume_safety_fails_if_ad_mismatch(self):
        """Rejects boot volume if its AD differs from target AD."""
        mock_bs = MagicMock()
        mock_comp = MagicMock()

        bv = MagicMock()
        bv.lifecycle_state = "AVAILABLE"
        bv.availability_domain = "kONR:AP-MUMBAI-1-AD-2"  # Different AD
        bv.display_name = "kron-ai"
        mock_bs.get_boot_volume.return_value.data = bv

        with self.assertRaises(SafetyCheckError) as ctx:
            verify_boot_volume_safety(
                mock_bs, mock_comp, "comp_123", "bv_123", "kONR:AP-MUMBAI-1-AD-1"
            )
        self.assertIn("Availability Domain mismatch", str(ctx.exception))

    def test_boot_volume_safety_fails_if_attached(self):
        """Rejects launch if boot volume is currently attached to an active instance."""
        mock_bs = MagicMock()
        mock_comp = MagicMock()

        bv = MagicMock()
        bv.lifecycle_state = "AVAILABLE"
        bv.availability_domain = "kONR:AP-MUMBAI-1-AD-1"
        bv.display_name = "kron-ai"
        mock_bs.get_boot_volume.return_value.data = bv

        attachment = MagicMock()
        attachment.lifecycle_state = "ATTACHED"
        attachment.instance_id = "ocid1.instance.oc1..some_other_instance"
        mock_comp.list_boot_volume_attachments.return_value.data = [attachment]

        with self.assertRaises(SafetyCheckError) as ctx:
            verify_boot_volume_safety(
                mock_bs, mock_comp, "comp_123", "bv_123", "kONR:AP-MUMBAI-1-AD-1"
            )
        self.assertIn("currently attached to instance", str(ctx.exception))
        self.assertIn("NEVER detaches volumes automatically", str(ctx.exception))

    def test_boot_volume_safety_passes_when_detached_and_available(self):
        """Safety check succeeds when boot volume is AVAILABLE, detached, and in matching AD."""
        mock_bs = MagicMock()
        mock_comp = MagicMock()

        bv = MagicMock()
        bv.lifecycle_state = "AVAILABLE"
        bv.availability_domain = "kONR:AP-MUMBAI-1-AD-1"
        bv.display_name = "kron-ai"
        mock_bs.get_boot_volume.return_value.data = bv

        # No active attachments
        mock_comp.list_boot_volume_attachments.return_value.data = []

        # Should not raise exception
        verify_boot_volume_safety(
            mock_bs, mock_comp, "comp_123", "bv_123", "kONR:AP-MUMBAI-1-AD-1"
        )

    def test_subnet_safety_checks(self):
        """Subnet safety checks validate state and regional vs AD match."""
        mock_vn = MagicMock()
        subnet = MagicMock()
        subnet.lifecycle_state = "AVAILABLE"
        subnet.availability_domain = None  # Regional subnet
        subnet.display_name = "public-subnet-v1_kron"
        mock_vn.get_subnet.return_value.data = subnet

        # Regional subnet should succeed
        verify_subnet_safety(mock_vn, "subnet_123", "kONR:AP-MUMBAI-1-AD-1")

        # AD-mismatched subnet should raise SafetyCheckError
        subnet.availability_domain = "kONR:AP-MUMBAI-1-AD-2"
        with self.assertRaises(SafetyCheckError):
            verify_subnet_safety(mock_vn, "subnet_123", "kONR:AP-MUMBAI-1-AD-1")

    def test_launch_instance_uses_boot_volume_source(self):
        """Verifies that launch uses InstanceSourceViaBootVolumeDetails with correct parameters."""
        mock_compute = MagicMock()
        created_inst = MagicMock()
        created_inst.id = "ocid1.instance.oc1.ap-mumbai-1..newly_launched_vm"
        created_inst.display_name = "kron_new"
        created_inst.lifecycle_state = "PROVISIONING"
        created_inst.shape = "VM.Standard.A1.Flex"
        created_inst.availability_domain = "kONR:AP-MUMBAI-1-AD-1"
        created_inst.compartment_id = self.config.compartment_id
        mock_compute.launch_instance.return_value.data = created_inst

        result = launch_instance_from_boot_volume(
            mock_compute, self.config, "kONR:AP-MUMBAI-1-AD-1"
        )

        self.assertEqual(result.id, "ocid1.instance.oc1.ap-mumbai-1..newly_launched_vm")
        self.assertEqual(result.display_name, "kron_new")

        # Verify launch call arguments
        mock_compute.launch_instance.assert_called_once()
        call_arg = mock_compute.launch_instance.call_args[0][0]

        # Verify source details is InstanceSourceViaBootVolumeDetails
        self.assertIsInstance(
            call_arg.source_details,
            oci.core.models.InstanceSourceViaBootVolumeDetails,
        )
        self.assertEqual(call_arg.source_details.source_type, "bootVolume")
        self.assertEqual(call_arg.source_details.boot_volume_id, self.config.boot_volume_id)

        # Verify shape config
        self.assertEqual(call_arg.shape, "VM.Standard.A1.Flex")
        self.assertEqual(call_arg.shape_config.ocpus, 2.0)
        self.assertEqual(call_arg.shape_config.memory_in_gbs, 12.0)

        # Verify VNIC
        self.assertTrue(call_arg.create_vnic_details.assign_public_ip)
        self.assertEqual(call_arg.create_vnic_details.subnet_id, self.config.subnet_id)


if __name__ == "__main__":
    unittest.main()
