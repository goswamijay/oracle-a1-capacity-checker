"""
OCI Client management and error classification.
Handles authenticated OCI SDK client initialization and categorizes errors
into actionable types (CAPACITY, AUTHENTICATION, CONFIGURATION, RATE_LIMIT, TRANSIENT).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import oci

from src.config import Config

logger = logging.getLogger("oci_checker.oci_client")


class ErrorCategory(str, Enum):
    """Categorized failure types for clear decision-making."""

    CAPACITY = "CAPACITY"                # Out of capacity -> Retryable
    AUTHENTICATION = "AUTHENTICATION"    # Invalid key/signature/unauthorized -> Fatal stop
    CONFIGURATION = "CONFIGURATION"      # Invalid subnet/AD/volume/quota -> Fatal stop
    RATE_LIMIT = "RATE_LIMIT"            # 429 Too Many Requests -> Exponential backoff retry
    TRANSIENT = "TRANSIENT"              # 500/503 service blips -> Exponential backoff retry
    UNKNOWN = "UNKNOWN"                  # Unrecognized exception -> Inspect/Stop


@dataclass
class OCIClients:
    """Container holding authenticated OCI service clients."""

    compute_client: oci.core.ComputeClient
    blockstorage_client: oci.core.BlockstorageClient
    virtual_network_client: oci.core.VirtualNetworkClient
    identity_client: oci.identity.IdentityClient
    config_dict: Dict[str, Any]


def format_private_key(key_str: str) -> str:
    """Ensure RSA private key PEM has proper newlines and headers.
    
    Handles single-line environment variables containing literal '\\n' sequences.
    """
    clean_key = key_str.strip()

    # Convert literal \n sequences into real newlines if passed in single-line format
    if "\\n" in clean_key:
        clean_key = clean_key.replace("\\n", "\n")

    # If raw base64 without header/footer was supplied, wrap with standard PEM header/footer
    if "BEGIN" not in clean_key and "END" not in clean_key:
        clean_key = f"-----BEGIN RSA PRIVATE KEY-----\n{clean_key}\n-----END RSA PRIVATE KEY-----"

    return clean_key.strip() + "\n"


def create_oci_config(config: Config) -> Tuple[Dict[str, Any], oci.Signer]:
    """Create OCI SDK configuration dictionary and Signer from Config object.
    
    Supports both key_file and in-memory key_content without exposing secrets.
    """
    config_dict: Dict[str, Any] = {
        "user": config.user,
        "fingerprint": config.fingerprint,
        "tenancy": config.tenancy,
        "region": config.region,
    }

    if config.key_content:
        formatted_key = format_private_key(config.key_content)
        config_dict["key_content"] = formatted_key
        signer = oci.Signer(
            tenancy=config.tenancy,
            user=config.user,
            fingerprint=config.fingerprint,
            private_key_file_location=None,
            pass_phrase=None,
            private_key_content=formatted_key,
        )
    elif config.key_file:
        expanded_path = os.path.expanduser(config.key_file)
        if not Path(expanded_path).is_file():
            raise FileNotFoundError(f"OCI private key file not found at: {expanded_path}")
        config_dict["key_file"] = expanded_path
        signer = oci.Signer.from_config(config_dict)
    else:
        raise ValueError("Neither key_content nor key_file provided for OCI authentication.")

    return config_dict, signer


def get_oci_clients(config: Config) -> OCIClients:
    """Initialize and return all required OCI service clients."""
    config_dict, signer = create_oci_config(config)

    # Use standard client timeouts: 30s connect, 60s read
    client_kwargs = {
        "timeout": (30.0, 60.0),
        "signer": signer,
    }

    compute = oci.core.ComputeClient(config_dict, **client_kwargs)
    blockstorage = oci.core.BlockstorageClient(config_dict, **client_kwargs)
    virtual_network = oci.core.VirtualNetworkClient(config_dict, **client_kwargs)
    identity = oci.identity.IdentityClient(config_dict, **client_kwargs)

    return OCIClients(
        compute_client=compute,
        blockstorage_client=blockstorage,
        virtual_network_client=virtual_network,
        identity_client=identity,
        config_dict=config_dict,
    )


def classify_oci_error(error: Exception) -> Tuple[ErrorCategory, str]:
    """Classify an OCI exception into actionable categories.
    
    Returns:
        Tuple of (ErrorCategory, detailed human-readable message)
    """
    if not isinstance(error, oci.exceptions.ServiceError):
        # Generic python exception / network failure
        msg = str(error)
        lower_msg = msg.lower()
        if "connection" in lower_msg or "timeout" in lower_msg or "temporary" in lower_msg:
            return ErrorCategory.TRANSIENT, f"Network or transient transport error: {msg}"
        return ErrorCategory.UNKNOWN, f"Non-OCI unexpected error: {msg}"

    status = error.status
    code = error.code or ""
    message = error.message or ""
    lower_message = message.lower()
    lower_code = code.lower()

    # 1. Capacity detection
    # Common capacity error signatures:
    # "Out of capacity for shape VM.Standard.A1.Flex in availability domain AD-1"
    # "InternalError: Out of host capacity"
    # "Host capacity unavailable"
    capacity_signatures = [
        "out of capacity",
        "out of host capacity",
        "insufficient host capacity",
        "host capacity unavailable",
        "outofcapacity",
        "capacity unavailable",
    ]
    if any(sig in lower_message or sig in lower_code for sig in capacity_signatures):
        return ErrorCategory.CAPACITY, f"Capacity exhausted: {message}"

    # 2. Authentication detection (Fatal)
    # 401 Not Authenticated, 403 Not Authorized, Invalid Signature
    if status in (401, 403) or any(
        s in lower_code or s in lower_message
        for s in ("notauthenticated", "notauthorized", "invalidsignature", "authfailure", "credentials")
    ):
        return (
            ErrorCategory.AUTHENTICATION,
            f"Authentication/Authorization failed ({status} - {code}): {message}. "
            f"Check OCI_USER, OCI_FINGERPRINT, OCI_TENANCY, and API private key.",
        )

    # 3. Rate limiting (Retryable)
    if status == 429 or "toomanyrequests" in lower_code or "rate limit" in lower_message:
        return ErrorCategory.RATE_LIMIT, f"Rate limited by OCI ({code}): {message}"

    # 4. Service Quota or Limit Exceeded (Fatal - not capacity)
    if "quotaexceeded" in lower_code or "limitexceeded" in lower_code:
        return (
            ErrorCategory.CONFIGURATION,
            f"Account service limit or quota exceeded ({code}): {message}. "
            f"Verify your tenancy limit for A1 Flex OCPUs/memory.",
        )

    # 5. Configuration issues (Fatal)
    # 400 Bad Request, 404 Not Found (e.g. invalid subnet, wrong AD, invalid compartment)
    if status in (400, 404):
        return (
            ErrorCategory.CONFIGURATION,
            f"Invalid configuration ({status} - {code}): {message}. "
            f"Check Compartment, Availability Domain, Subnet OCID, or Shape details.",
        )

    # 6. Transient / 5xx Server Blips (Retryable)
    if status in (500, 502, 503, 504):
        return ErrorCategory.TRANSIENT, f"OCI transient service issue ({status} - {code}): {message}"

    return ErrorCategory.UNKNOWN, f"Unhandled OCI ServiceError ({status} - {code}): {message}"
