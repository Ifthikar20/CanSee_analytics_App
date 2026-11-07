"""
Discord Webhook Service — Send rich embed messages to Discord channels.
"""
import logging
from datetime import UTC, datetime

import requests

logger = logging.getLogger("apps")

# Cansee brand color
BRAND_COLOR = 0x8b5cf6  # Purple

DISCORD_API_BASE = "https://discord.com/api/v10"


def _build_embed(*, title: str = "", description: str = "",
                 fields: list = None, footer: str = "", color: int = None) -> dict:
    """Build a single Discord embed dict from the common message parts."""
    embed = {
        "color": color or BRAND_COLOR,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    if title:
        embed["title"] = title
    if description:
        embed["description"] = description
    if fields:
        embed["fields"] = fields
    if footer:
        embed["footer"] = {"text": footer}
    return embed


class DiscordService:
    @staticmethod
    def send_message(*, webhook_url: str, title: str = "", description: str = "",
                     fields: list = None, footer: str = "", color: int = None) -> bool:
        """Send a rich embed message to a Discord webhook."""
        if not webhook_url:
            return False

        payload = {
            "username": "Cansee",
            "embeds": [_build_embed(
                title=title, description=description,
                fields=fields, footer=footer, color=color,
            )],
        }

        try:
            response = requests.post(webhook_url, json=payload, timeout=10)
            return response.status_code in (200, 204)
        except Exception as e:
            logger.error(f"Discord notification failed: {e}")
            return False

    @staticmethod
    def send_followup(*, application_id: str, interaction_token: str,
                      content: str = "", title: str = "", description: str = "",
                      fields: list = None, footer: str = "", color: int = None) -> bool:
        """Send an interaction follow-up message (after a deferred response).

        Posts to the interaction webhook, which is valid for 15 minutes
        after the interaction. Pass ``content`` for a plain message, or
        embed parts (``title``/``description``/``fields``) for a rich one.
        """
        if not application_id or not interaction_token:
            logger.warning("Discord follow-up skipped: missing application id or interaction token.")
            return False

        url = f"{DISCORD_API_BASE}/webhooks/{application_id}/{interaction_token}"
        payload = {}
        if title or description or fields:
            payload["embeds"] = [_build_embed(
                title=title, description=description,
                fields=fields, footer=footer, color=color,
            )]
        if content:
            payload["content"] = content
        if not payload:
            return False

        for attempt in (0, 1):
            try:
                response = requests.post(url, json=payload, timeout=10)
            except Exception as e:
                logger.error(f"Discord follow-up failed: {e}")
                return False
            if response.status_code in (200, 204):
                return True
            # A follow-up can race the initial ack: Unknown Webhook right
            # after the interaction means the ack has not landed yet, so
            # give it a second and retry once.
            if (
                attempt == 0
                and response.status_code == 404
                and '"code": 10015' in response.text
            ):
                import time
                time.sleep(1.0)
                continue
            logger.warning(
                f"Discord follow-up failed with status {response.status_code}: "
                f"{response.text[:200]}"
            )
            return False
        return False
