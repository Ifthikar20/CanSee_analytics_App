import logging

import requests
from django.conf import settings

logger = logging.getLogger("apps")

SLACK_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"


class SlackService:
    @staticmethod
    def send_message(*, webhook_url: str, text: str, blocks: list = None) -> bool:
        """Send a message to a Slack webhook."""
        if not webhook_url:
            return False

        payload = {"text": text}
        if blocks:
            payload["blocks"] = blocks

        try:
            response = requests.post(webhook_url, json=payload, timeout=10)
            return response.status_code == 200
        except Exception as e:
            logger.error(f"Slack notification failed: {e}")
            return False

    @staticmethod
    def post_message(*, channel: str, text: str, thread_ts: str = "", blocks: list = None) -> bool:
        """Post to a channel via chat.postMessage using the bot token.

        Used for app_mention replies, where there is no response_url to
        answer through. Skips gracefully when SLACK_BOT_TOKEN is unset.
        """
        token = getattr(settings, "SLACK_BOT_TOKEN", "")
        if not token:
            logger.warning("SLACK_BOT_TOKEN not configured - skipping chat.postMessage delivery.")
            return False
        if not channel:
            return False

        payload = {"channel": channel, "text": text}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        if blocks:
            payload["blocks"] = blocks

        try:
            response = requests.post(
                SLACK_POST_MESSAGE_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                timeout=10,
            )
            data = response.json()
            if not data.get("ok"):
                logger.warning(f"Slack chat.postMessage failed: {data.get('error', 'unknown')}")
                return False
            return True
        except Exception as e:
            logger.error(f"Slack chat.postMessage failed: {e}")
            return False
