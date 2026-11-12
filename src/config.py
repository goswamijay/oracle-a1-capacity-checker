"""
Configuration management for Oracle Cloud A1 Flex Capacity Checker & VM Creator.
Loads settings from environment variables and .env files with safety validations.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional


def _mask_secret(val: Optional[str], visible_chars: int = 4) -> str:
    """Safely mask secret strings for logging."""
    if not val:
        return "<NOT_SET>"
    val = val.strip()
    if len(val) <= visible_chars * 2:
        return "********"
    return f"{val[:visible_chars]}...{val[-visible_chars:]}"


@dataclass
class Config:
    """Application configuration parameters."""

    # OCI Credentials
    tenancy: str
    user: str
    fingerprint: str
    region: str
    key_file: Optional[str] = None
    key_content: Optional[str] = None

    # Target Infrastructure OCIDs
    compartment_id: str = ""
    availability_domain: str = ""
    subnet_id: str = ""
    boot_volume_id: str = ""

    # Instance Specifications
    instance_name: str = "kron_new"
    shape: str = "VM.Standard.A1.Flex"
    ocpus: float = 2.0
    memory_in_gbs: float = 12.0
    assign_public_ip: bool = True

    # Execution controls
    retry_interval_seconds: int = 60
    max_retries: Optional[int] = None
    max_duration_seconds: Optional[int] = None
    check_capacity_report_first: bool = True

    # Notifications (Optional)
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None

    def validate(self) -> None:
        """Validate required configuration values.
        
        Raises:
            ValueError: If any required parameter is missing or invalid.
        """
        required_fields = {
            "OCI_TENANCY": self.tenancy,
            "OCI_USER": self.user,
            "OCI_FINGERPRINT": self.fingerprint,
            "OCI_REGION": self.region,
            "OCI_COMPARTMENT": self.compartment_id,
            "OCI_AD": self.availability_domain,
            "OCI_SUBNET": self.subnet_id,
            "OCI_BOOT_VOLUME": self.boot_volume_id,
        }

        missing = []
        for env_name, val in required_fields.items():
            if not val or not str(val).strip():
                missing.append(env_name)
            elif str(val).strip().startswith("YOUR_") or str(val).strip().endswith("_HERE"):
                missing.append(f"{env_name} (contains placeholder value: '{val}')")

        if not self.key_file and not self.key_content:
            missing.append("OCI_PRIVATE_KEY or OCI_KEY_FILE (at least one must be provided)")

        if missing:
            raise ValueError(
                f"Missing or invalid required configuration parameters:\n - "
                + "\n - ".join(missing)
            )

        if self.ocpus <= 0:
            raise ValueError(f"OCPUs must be greater than 0, got {self.ocpus}")
        if self.memory_in_gbs <= 0:
            raise ValueError(f"memory_in_gbs must be greater than 0, got {self.memory_in_gbs}")
        if self.retry_interval_seconds <= 0:
            raise ValueError(f"retry_interval_seconds must be positive, got {self.retry_interval_seconds}")

    def to_safe_dict(self) -> Dict[str, Any]:
        """Return a dictionary safe for printing and logging with secrets masked."""
        return {
            "tenancy": _mask_secret(self.tenancy, 12),
            "user": _mask_secret(self.user, 12),
            "fingerprint": _mask_secret(self.fingerprint, 8),
            "region": self.region,
            "has_key_content": bool(self.key_content),
            "key_file": self.key_file or "<NONE>",
            "compartment_id": _mask_secret(self.compartment_id, 12),
            "availability_domain": self.availability_domain,
            "subnet_id": _mask_secret(self.subnet_id, 12),
            "boot_volume_id": _mask_secret(self.boot_volume_id, 12),
            "instance_name": self.instance_name,
            "shape": self.shape,
            "ocpus": self.ocpus,
            "memory_in_gbs": self.memory_in_gbs,
            "assign_public_ip": self.assign_public_ip,
            "retry_interval_seconds": self.retry_interval_seconds,
            "max_retries": self.max_retries,
            "max_duration_seconds": self.max_duration_seconds,
            "check_capacity_report_first": self.check_capacity_report_first,
            "telegram_configured": bool(self.telegram_bot_token and self.telegram_chat_id),
        }


def load_config(env_file: Optional[str] = None) -> Config:
    """Load configuration from environment variables, optionally reading an .env file."""
    # Attempt dotenv load if available and file exists
    if env_file and Path(env_file).is_file():
        try:
            import dotenv
            dotenv.load_dotenv(env_file, override=False)
        except ImportError:
            # Fallback simple env parser if python-dotenv is not installed
            _load_env_fallback(env_file)
    else:
        # Check standard .env in current directory
        if Path(".env").is_file():
            try:
                import dotenv
                dotenv.load_dotenv(".env", override=False)
            except ImportError:
                _load_env_fallback(".env")

    # Read from environment variables
    tenancy = os.getenv("OCI_TENANCY", "").strip()
    user = os.getenv("OCI_USER", "").strip()
    fingerprint = os.getenv("OCI_FINGERPRINT", "").strip()
    region = os.getenv("OCI_REGION", "ap-mumbai-1").strip()
    key_file = os.getenv("OCI_KEY_FILE", "").strip() or None
    key_content = os.getenv("OCI_PRIVATE_KEY", "").strip() or None

    compartment_id = os.getenv("OCI_COMPARTMENT", "").strip() or tenancy
    availability_domain = os.getenv("OCI_AD", "AD-1").strip()
    subnet_id = os.getenv("OCI_SUBNET", "").strip()
    boot_volume_id = os.getenv("OCI_BOOT_VOLUME", "").strip()

    instance_name = os.getenv("INSTANCE_NAME", "kron_new").strip()
    shape = os.getenv("INSTANCE_SHAPE", "VM.Standard.A1.Flex").strip()

    try:
        ocpus = float(os.getenv("OCPUS", "2"))
    except ValueError:
        ocpus = 2.0

    try:
        memory_in_gbs = float(os.getenv("MEMORY_IN_GBS", "12"))
    except ValueError:
        memory_in_gbs = 12.0

    assign_public_ip = os.getenv("ASSIGN_PUBLIC_IP", "true").lower() in ("true", "1", "yes")

    try:
        retry_interval = int(os.getenv("RETRY_INTERVAL_SECONDS", "60"))
    except ValueError:
        retry_interval = 60

    max_retries_env = os.getenv("MAX_RETRIES", "").strip()
    max_retries = int(max_retries_env) if max_retries_env.isdigit() else None

    max_duration_env = os.getenv("MAX_DURATION_SECONDS", "").strip()
    max_duration_seconds = int(max_duration_env) if max_duration_env.isdigit() else None

    check_capacity_report_first = os.getenv("CHECK_CAPACITY_REPORT_FIRST", "true").lower() in ("true", "1", "yes")

    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or None
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip() or None

    return Config(
        tenancy=tenancy,
        user=user,
        fingerprint=fingerprint,
        region=region,
        key_file=key_file,
        key_content=key_content,
        compartment_id=compartment_id,
        availability_domain=availability_domain,
        subnet_id=subnet_id,
        boot_volume_id=boot_volume_id,
        instance_name=instance_name,
        shape=shape,
        ocpus=ocpus,
        memory_in_gbs=memory_in_gbs,
        assign_public_ip=assign_public_ip,
        retry_interval_seconds=retry_interval,
        max_retries=max_retries,
        max_duration_seconds=max_duration_seconds,
        check_capacity_report_first=check_capacity_report_first,
        telegram_bot_token=telegram_bot_token,
        telegram_chat_id=telegram_chat_id,
    )


def _load_env_fallback(file_path: str) -> None:
    """Minimal fallback loader for .env files without requiring external packages."""
    path = Path(file_path)
    if not path.is_file():
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip("\"'")
            if key and key not in os.environ:
                os.environ[key] = val
