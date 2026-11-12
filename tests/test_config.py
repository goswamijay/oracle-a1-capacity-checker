"""
Unit tests for configuration loading, validation, and secret masking.
"""

import os
import unittest
from unittest.mock import patch

from src.config import Config, _mask_secret, load_config


class TestConfig(unittest.TestCase):
    """Test suite for Config dataclass and loader."""

    def setUp(self):
        self.valid_env = {
            "OCI_TENANCY": "ocid1.tenancy.oc1..aaaaaaaaxxxxyyyyzzzz",
            "OCI_USER": "ocid1.user.oc1..aaaaaaaaxxxxyyyyzzzz",
            "OCI_FINGERPRINT": "20:3b:97:11:22:33:44:55:66:77:88:99:aa:bb:cc:dd",
            "OCI_REGION": "ap-mumbai-1",
            "OCI_PRIVATE_KEY": "-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----",
            "OCI_COMPARTMENT": "ocid1.compartment.oc1..aaaaaaaaxxxxyyyyzzzz",
            "OCI_AD": "AD-1",
            "OCI_SUBNET": "ocid1.subnet.oc1.ap-mumbai-1..aaaaaaaaxxxxyyyyzzzz",
            "OCI_BOOT_VOLUME": "ocid1.bootvolume.oc1.ap-mumbai-1..aaaaaaaaxxxxyyyyzzzz",
            "INSTANCE_NAME": "kron_new",
            "INSTANCE_SHAPE": "VM.Standard.A1.Flex",
            "OCPUS": "2",
            "MEMORY_IN_GBS": "12",
        }

    def test_valid_config_validation(self):
        """Valid configuration passes validation without raising exceptions."""
        cfg = Config(
            tenancy="ocid1.tenancy.oc1..12345",
            user="ocid1.user.oc1..12345",
            fingerprint="aa:bb:cc:dd",
            region="ap-mumbai-1",
            key_content="dummy_key_content",
            compartment_id="ocid1.compartment.oc1..12345",
            availability_domain="AD-1",
            subnet_id="ocid1.subnet.oc1..12345",
            boot_volume_id="ocid1.bootvolume.oc1..12345",
            ocpus=2.0,
            memory_in_gbs=12.0,
        )
        # Should not raise
        cfg.validate()

    def test_missing_required_field_raises_value_error(self):
        """Missing required OCID fields raises ValueError."""
        cfg = Config(
            tenancy="",
            user="ocid1.user.oc1..12345",
            fingerprint="aa:bb:cc:dd",
            region="ap-mumbai-1",
            key_content="dummy_key_content",
            compartment_id="ocid1.compartment.oc1..12345",
            availability_domain="AD-1",
            subnet_id="ocid1.subnet.oc1..12345",
            boot_volume_id="ocid1.bootvolume.oc1..12345",
        )
        with self.assertRaises(ValueError) as ctx:
            cfg.validate()
        self.assertIn("OCI_TENANCY", str(ctx.exception))

    def test_placeholder_value_detected(self):
        """Config detects unconfigured placeholder values like 'YOUR_TENANCY_OCID'."""
        cfg = Config(
            tenancy="YOUR_TENANCY_OCID_HERE",
            user="ocid1.user.oc1..12345",
            fingerprint="aa:bb:cc:dd",
            region="ap-mumbai-1",
            key_content="dummy_key_content",
            compartment_id="ocid1.compartment.oc1..12345",
            availability_domain="AD-1",
            subnet_id="ocid1.subnet.oc1..12345",
            boot_volume_id="ocid1.bootvolume.oc1..12345",
        )
        with self.assertRaises(ValueError) as ctx:
            cfg.validate()
        self.assertIn("placeholder value", str(ctx.exception))

    def test_invalid_ocpu_or_memory(self):
        """Zero or negative resource allocations raise ValueError."""
        cfg = Config(
            tenancy="ocid1.tenancy.oc1..12345",
            user="ocid1.user.oc1..12345",
            fingerprint="aa:bb:cc:dd",
            region="ap-mumbai-1",
            key_content="dummy_key_content",
            compartment_id="ocid1.compartment.oc1..12345",
            availability_domain="AD-1",
            subnet_id="ocid1.subnet.oc1..12345",
            boot_volume_id="ocid1.bootvolume.oc1..12345",
            ocpus=0,
            memory_in_gbs=12.0,
        )
        with self.assertRaises(ValueError):
            cfg.validate()

    def test_secret_masking(self):
        """to_safe_dict masks private keys and OCIDs properly."""
        cfg = Config(
            tenancy="ocid1.tenancy.oc1..secret_tenancy_data",
            user="ocid1.user.oc1..secret_user_data",
            fingerprint="20:3b:97:11:22:33:44:55:66:77:88:99",
            region="ap-mumbai-1",
            key_content="PRIVATE_RSA_KEY_DATA_SHOULD_NEVER_BE_LOGGED",
            compartment_id="ocid1.compartment.oc1..secret_compartment",
            availability_domain="AD-1",
            subnet_id="ocid1.subnet.oc1..secret_subnet",
            boot_volume_id="ocid1.bootvolume.oc1..secret_volume",
            telegram_bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            telegram_chat_id="987654321",
        )
        safe = cfg.to_safe_dict()
        # Verify private key string is not present in the output
        self.assertNotIn("PRIVATE_RSA_KEY_DATA_SHOULD_NEVER_BE_LOGGED", str(safe))
        # Verify secrets are masked
        self.assertTrue(safe["has_key_content"])
        self.assertTrue(safe["telegram_configured"])
        self.assertIn("...", safe["tenancy"])
        self.assertIn("...", safe["subnet_id"])

    @patch.dict(os.environ, {}, clear=True)
    def test_load_config_from_environ(self):
        """load_config reads directly from os.environ properly."""
        with patch.dict(os.environ, self.valid_env):
            cfg = load_config()
            self.assertEqual(cfg.region, "ap-mumbai-1")
            self.assertEqual(cfg.ocpus, 2.0)
            self.assertEqual(cfg.memory_in_gbs, 12.0)
            self.assertEqual(cfg.instance_name, "kron_new")
            cfg.validate()


if __name__ == "__main__":
    unittest.main()
