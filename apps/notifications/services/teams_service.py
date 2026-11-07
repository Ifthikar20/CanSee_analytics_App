"""Microsoft Teams outbound service — post messages to a Teams channel
through an incoming webhook.

Teams has two webhook flavours, and a pasted URL can be either:

* the classic **Incoming Webhook** connector (``*.webhook.office.com``),
  which accepts a legacy *MessageCard* payload; and
* the newer **Workflows** trigger (``*.logic.azure.com``), which accepts
  an *Adaptive Card* wrapped in a ``message`` attachment.

``send_message`` picks the payload from the URL host so either kind of
webhook a user pastes just works. Like the Slack/Discord incoming-webhook
senders, the URL itself is the credential — there is no bot token here.
(The two-way command bot is a separate Bot Framework surface.)
"""
import logging
from urllib.parse import urlparse

import requests

logger = logging.getLogger("apps")

# Cansee brand purple, as a hex string (MessageCard themeColor is RRGGBB).
BRAND_COLOR = "8B5CF6"
# Teams renders long cards fine, but keep a sane ceiling.
TEAMS_TEXT_LIMIT = 8000


def _is_workflow_url(webhook_url: str) -> bool:
    host = (urlparse(webhook_url).hostname or "").lower()
    return "logic.azure.com" in host


def _adaptive_card_payload(title: str, text: str) -> dict:
    """Workflows (Power Automate) incoming webhook: an Adaptive Card wrapped
    in a message attachment."""
    body = []
    if title:
        body.append({
            "type": "TextBlock", "text": title,
            "weight": "Bolder", "size": "Medium", "wrap": True,
        })
    if text:
        body.append({"type": "TextBlock", "text": text, "wrap": True})
    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "type": "AdaptiveCard",
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "version": "1.4",
                "body": body,
            },
        }],
    }


def _message_card_payload(title: str, text: str) -> dict:
    """Classic Office 365 connector incoming webhook: a MessageCard.

    ``text`` supports basic markdown (including ``- `` bullets); blank
    lines separate paragraphs.
    """
    card = {
        "@type": "MessageCard",
        "@context": "https://schema.org/extensions",
        "summary": title or "Cansee",
        "themeColor": BRAND_COLOR,
        "text": text,
    }
    if title:
        card["title"] = title
    return card


class TeamsService:
    @staticmethod
    def send_message(*, webhook_url: str, title: str = "", text: str = "") -> bool:
        """Post a message to a Teams incoming webhook. Returns True on success."""
        if not webhook_url:
            return False

        text = (text or "")[:TEAMS_TEXT_LIMIT]
        payload = (
            _adaptive_card_payload(title, text)
            if _is_workflow_url(webhook_url)
            else _message_card_payload(title, text)
        )

        try:
            response = requests.post(webhook_url, json=payload, timeout=10)
        except Exception as e:
            logger.error(f"Teams notification failed: {e}")
            return False
        # Connectors return 200; Workflows triggers accept with 202.
        if response.status_code in (200, 202, 204):
            return True
        logger.warning(
            f"Teams webhook returned {response.status_code}: {response.text[:200]}"
        )
        return False
