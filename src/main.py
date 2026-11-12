"""
Main orchestrator for Oracle Cloud A1 Flex Capacity Checker & VM Creator.
Handles CLI arguments, bounded CI loops, continuous daemon execution,
duplicate detection, safety checks, and authoritative launch attempts.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Optional

# Ensure project root is in sys.path for direct script execution
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import oci

from src.capacity_checker import check_compute_capacity_report
from src.config import Config, load_config
from src.instance_manager import (
    SafetyCheckError,
    check_existing_instance,
    launch_instance_from_boot_volume,
    resolve_availability_domain,
    save_state,
    verify_boot_volume_safety,
    verify_subnet_safety,
)
from src.notifier import TelegramNotifier
from src.oci_client import ErrorCategory, classify_oci_error, get_oci_clients

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("oci_checker.main")


def parse_arguments() -> argparse.Namespace:
    """Parse command line flags."""
    parser = argparse.ArgumentParser(
        description="Oracle Cloud A1 Flex Capacity Checker & Automatic VM Creator"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Execute a single check/launch attempt and exit immediately.",
    )
    parser.add_argument(
        "--daemon",
        "--continuous",
        action="store_true",
        help="Run continuously in a loop until instance is successfully created (ideal for VPS).",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help="Seconds to wait between retry attempts (overrides RETRY_INTERVAL_SECONDS, default: 60).",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=None,
        help="Maximum retry attempts before exiting (default: unlimited or controlled by duration).",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=None,
        help="Maximum total run time in seconds before exiting gracefully (ideal for GitHub Actions).",
    )
    parser.add_argument(
        "--env-file",
        type=str,
        default=None,
        help="Path to custom .env file.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose debug logging.",
    )
    return parser.parse_args()


def run_checker(config: Config, args: argparse.Namespace) -> int:
    """Execute the capacity check and VM launch lifecycle.
    
    Returns exit code:
      0: Instance successfully launched OR already exists
      1: Fatal error (configuration or authentication failure)
      2: Exited after max retries/duration without capacity (retryable state)
    """
    notifier = TelegramNotifier(config.telegram_bot_token, config.telegram_chat_id)

    # 1. Print safe startup summary
    logger.info("================================================================")
    logger.info("   Oracle Cloud A1 Flex Capacity Checker & VM Creator")
    logger.info("================================================================")
    safe_cfg = config.to_safe_dict()
    for k, v in safe_cfg.items():
        logger.info("Config -> %-28s: %s", k, v)
    logger.info("----------------------------------------------------------------")

    # 2. Initialize OCI SDK clients
    try:
        clients = get_oci_clients(config)
    except Exception as exc:
        cat, msg = classify_oci_error(exc)
        logger.error("Failed to initialize OCI clients: %s", msg)
        notifier.notify_fatal_error(cat.value, msg)
        return 1

    # 3. Resolve Availability Domain name
    try:
        resolved_ad = resolve_availability_domain(
            clients.identity_client,
            config.compartment_id,
            config.availability_domain,
        )
        logger.info("Resolved Availability Domain: %s", resolved_ad)
    except SafetyCheckError as exc:
        logger.error("Availability Domain resolution failed: %s", exc)
        notifier.notify_fatal_error(ErrorCategory.CONFIGURATION.value, str(exc))
        return 1
    except Exception as exc:
        cat, msg = classify_oci_error(exc)
        logger.error("Error during AD resolution: %s", msg)
        notifier.notify_fatal_error(cat.value, msg)
        return 1

    # 4. Duplicate Check: Search for existing instance
    try:
        existing_inst = check_existing_instance(
            clients.compute_client,
            config.compartment_id,
            config.instance_name,
        )
        if existing_inst:
            logger.info("================================================================")
            logger.info("TARGET INSTANCE ALREADY EXISTS!")
            logger.info("Name: %s", existing_inst.display_name)
            logger.info("OCID: %s", existing_inst.id)
            logger.info("State: %s", existing_inst.lifecycle_state)
            logger.info("No further launch attempts needed. Automation complete.")
            logger.info("================================================================")
            save_state(existing_inst)
            notifier.notify_launch_success(
                instance_name=existing_inst.display_name,
                instance_id=existing_inst.id,
                lifecycle_state=existing_inst.lifecycle_state,
                region=config.region,
                ad=existing_inst.availability_domain,
                ocpus=config.ocpus,
                ram_gb=config.memory_in_gbs,
            )
            return 0
    except Exception as exc:
        cat, msg = classify_oci_error(exc)
        logger.error("Failed during duplicate instance verification: %s", msg)
        notifier.notify_fatal_error(cat.value, msg)
        return 1

    # 5. Boot Volume & Subnet Safety Validations
    try:
        verify_boot_volume_safety(
            clients.blockstorage_client,
            clients.compute_client,
            config.compartment_id,
            config.boot_volume_id,
            resolved_ad,
        )
        verify_subnet_safety(
            clients.virtual_network_client,
            config.subnet_id,
            resolved_ad,
        )
    except SafetyCheckError as exc:
        logger.critical("INFRASTRUCTURE SAFETY CHECK FAILED: %s", exc)
        notifier.notify_fatal_error(ErrorCategory.CONFIGURATION.value, str(exc))
        return 1
    except Exception as exc:
        cat, msg = classify_oci_error(exc)
        logger.critical("Unexpected error during safety checks: %s", msg)
        notifier.notify_fatal_error(cat.value, msg)
        return 1

    # 6. Retry Loop Execution
    attempt = 0
    start_time = time.time()
    interval = args.interval or config.retry_interval_seconds
    max_retries = args.max_retries or config.max_retries
    max_duration = args.duration or config.max_duration_seconds

    logger.info(
        "Starting monitoring loop: Interval=%ds | Max Retries=%s | Max Duration=%s",
        interval,
        str(max_retries) if max_retries else "unlimited",
        f"{max_duration}s" if max_duration else "unlimited",
    )

    while True:
        attempt += 1
        elapsed = time.time() - start_time
        logger.info(
            ">>> [Attempt #%d | Elapsed: %.0fs] Checking capacity for %s (%.1f OCPUs / %.1f GB) in %s...",
            attempt,
            elapsed,
            config.shape,
            config.ocpus,
            config.memory_in_gbs,
            resolved_ad,
        )

        # Optional advisory capacity report
        if config.check_capacity_report_first:
            check_compute_capacity_report(
                clients.compute_client,
                config.compartment_id,
                resolved_ad,
                config.shape,
                config.ocpus,
                config.memory_in_gbs,
            )

        # Authoritative Launch Attempt
        try:
            logger.info("Executing authoritative launch request with existing boot volume...")
            created_instance = launch_instance_from_boot_volume(
                clients.compute_client,
                config,
                resolved_ad,
            )

            # SUCCESS!
            logger.info("================================================================")
            logger.info("🎉 SUCCESS! INSTANCE CREATED AUTOMATICALLY BY OCI!")
            logger.info("Instance Display Name : %s", created_instance.display_name)
            logger.info("Instance OCID         : %s", created_instance.id)
            logger.info("Lifecycle State       : %s", created_instance.lifecycle_state)
            logger.info("Shape                 : %s", created_instance.shape)
            logger.info("Availability Domain   : %s", created_instance.availability_domain)
            logger.info("================================================================")

            save_state(created_instance)
            notifier.notify_launch_success(
                instance_name=created_instance.display_name,
                instance_id=created_instance.id,
                lifecycle_state=created_instance.lifecycle_state,
                region=config.region,
                ad=created_instance.availability_domain,
                ocpus=config.ocpus,
                ram_gb=config.memory_in_gbs,
            )
            return 0

        except oci.exceptions.ServiceError as exc:
            category, explanation = classify_oci_error(exc)
            logger.warning("[Attempt #%d Result] Category: %s | Reason: %s", attempt, category.value, explanation)

            if category == ErrorCategory.CAPACITY:
                # Capacity unavailable: this is expected, retry later
                pass
            elif category == ErrorCategory.AUTHENTICATION:
                logger.critical("Fatal authentication error encountered. Aborting.")
                notifier.notify_fatal_error(category.value, explanation)
                return 1
            elif category == ErrorCategory.CONFIGURATION:
                logger.critical("Fatal configuration error encountered. Aborting.")
                notifier.notify_fatal_error(category.value, explanation)
                return 1
            elif category in (ErrorCategory.RATE_LIMIT, ErrorCategory.TRANSIENT):
                logger.warning("Transient service/rate-limit error. Will retry with backoff.")
                time.sleep(10)
            else:
                logger.warning("Unhandled error category: %s. Continuing retry loop.", category.value)

        except Exception as exc:
            logger.error("[Attempt #%d] Unexpected exception: %s", attempt, str(exc))

        # Check loop termination conditions
        if args.once:
            logger.info("--once requested. Exiting after single attempt.")
            return 2

        if max_retries and attempt >= max_retries:
            logger.info("Reached maximum configured retries (%d). Exiting run.", max_retries)
            return 2

        if max_duration and (time.time() - start_time + interval) > max_duration:
            logger.info(
                "Execution time nearing maximum duration (%.0fs / %ds). Gracefully ending this run.",
                time.time() - start_time,
                max_duration,
            )
            return 2

        logger.info("Waiting %d seconds before next attempt...", interval)
        time.sleep(interval)


def main() -> None:
    """Entry point for CLI execution."""
    args = parse_arguments()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        config = load_config(args.env_file)
        config.validate()
    except Exception as exc:
        logger.critical("Configuration validation error: %s", exc)
        sys.exit(1)

    exit_code = run_checker(config, args)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
