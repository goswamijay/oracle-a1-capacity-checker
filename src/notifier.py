"""
Telegram notification module for Oracle Cloud Capacity Checker.
Provides optional real-time status alerts for capacity events, successful launch,
and critical fatal errors. Gracefully skips if credentials are not configured.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from typing import Optional

logger = logging.getLogger("oci_checker.notifier")


class TelegramNotifier:
    """Dispatches alerts to a Telegram chat using Telegram Bot API."""

    def __init__(self, bot_token: Optional[str] = None, chat_id: Optional[str] = None) -> None:
        self.bot_token = (bot_token or "").strip()
        self.chat_id = (chat_id or "").strip()
        self.enabled = bool(self.bot_token and self.chat_id)

        if not self.enabled:
            logger.debug("Telegram notifications are disabled (credentials not provided).")

    def send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        """Send a message to the configured Telegram chat.
        
        Returns:
            bool: True if sent successfully, False otherwise.
        """
        if not self.enabled:
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }

        try:
            req_data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=req_data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    logger.info("Telegram notification sent successfully.")
                    return True
                logger.warning("Telegram API responded with HTTP status %s", resp.status)
                return False
        except Exception as exc:
            logger.warning("Failed to send Telegram notification: %s", exc)
            return False

    def notify_capacity_detected(self, instance_name: str, shape: str, ad: str) -> None:
        """Alert when capacity becomes available and launch is initiated."""
        msg = (
            f"🚀 <b>OCI Capacity Detected!</b>\n\n"
            f"• <b>Instance:</b> <code>{instance_name}</code>\n"
            f"• <b>Shape:</b> <code>{shape}</code>\n"
            f"• <b>Availability Domain:</b> <code>{ad}</code>\n"
            f"• <b>Status:</b> Initiating launch now..."
        )
        self.send_message(msg)

    def notify_launch_success(
        self,
        instance_name: str,
        instance_id: str,
        lifecycle_state: str,
        region: str,
        ad: str,
        ocpus: float,
        ram_gb: float,
    ) -> None:
        """Alert on successful instance launch."""
        msg = (
            f"🎉 <b>OCI Instance Created Successfully!</b>\n\n"
            f"• <b>Name:</b> <code>{instance_name}</code>\n"
            f"• <b>State:</b> <code>{lifecycle_state}</code>\n"
            f"• <b>OCID:</b> <code>{instance_id}</code>\n"
            f"• <b>Region:</b> <code>{region}</code>\n"
            f"• <b>Availability Domain:</b> <code>{ad}</code>\n"
            f"• <b>Specs:</b> {ocpus:.0f} OCPU / {ram_gb:.0f} GB RAM\n\n"
            f"<i>The automation has completed its objective and will stop checking.</i>"
        )
        self.send_message(msg)

    def notify_fatal_error(self, category: str, error_message: str) -> None:
        """Alert when a non-retryable configuration or authentication error occurs."""
        msg = (
            f"❌ <b>OCI Capacity Checker: Fatal Error</b>\n\n"
            f"• <b>Category:</b> <code>{category}</code>\n"
            f"• <b>Reason:</b> <code>{error_message}</code>\n\n"
            f"<i>Execution stopped. Human intervention is required.</i>"
        )
        self.send_message(msg)
