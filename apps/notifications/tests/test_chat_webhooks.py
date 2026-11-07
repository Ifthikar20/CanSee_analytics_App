"""Tests for the inbound Slack/Discord chat webhooks."""
import hashlib
import hmac
import json
import time
from unittest.mock import patch
from urllib.parse import urlencode

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.test import Client

from apps.accounts.tests.factories import UserFactory
from apps.notifications.models import IntegrationConnection

DISCORD_URL = "/api/v1/notifications/discord/interactions/"
SLACK_EVENTS_URL = "/api/v1/notifications/slack/events/"
SLACK_COMMANDS_URL = "/api/v1/notifications/slack/commands/"

SIGNING_SECRET = "test-signing-secret"


def _connection(platform: str, team_id: str) -> IntegrationConnection:
    urls = {
        "slack": "https://hooks.slack.com/services/T0/B0/xyz",
        "discord": "https://discord.com/api/webhooks/1/abc",
    }
    return IntegrationConnection.objects.create(
        user=UserFactory(),
        platform=platform,
        webhook_url=urls[platform],
        external_team_id=team_id,
    )


# ── Discord interactions ──────────────────────────────────────────────────────


@pytest.fixture
def discord_key(settings):
    """Generate a keypair and configure its public half as the app's key."""
    private_key = Ed25519PrivateKey.generate()
    settings.DISCORD_PUBLIC_KEY = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()
    return private_key


def _post_interaction(private_key, payload: dict, *, signature: str = "",
                      timestamp: str = "1700000000"):
    raw = json.dumps(payload).encode("utf-8")
    if not signature:
        signature = private_key.sign(timestamp.encode("utf-8") + raw).hex()
    return Client().post(
        DISCORD_URL,
        data=raw,
        content_type="application/json",
        headers={
            "X-Signature-Ed25519": signature,
            "X-Signature-Timestamp": timestamp,
        },
    )


def _slash_payload(guild_id: str, subcommand: str = "report",
                   sub_options: list = None) -> dict:
    option = {"name": subcommand, "type": 1}
    if sub_options:
        option["options"] = sub_options
    return {
        "type": 2,
        "id": "1234567890",
        "token": "interaction-token",
        "guild_id": guild_id,
        "channel_id": "42",
        "member": {"user": {"username": "tester"}},
        "data": {"name": "cansee", "options": [option]},
    }


@pytest.mark.django_db
class TestDiscordInteractions:
    def test_ping_returns_pong(self, discord_key):
        resp = _post_interaction(discord_key, {"type": 1})
        assert resp.status_code == 200
        assert resp.json() == {"type": 1}

    def test_bad_signature_rejected(self, discord_key):
        other_key = Ed25519PrivateKey.generate()
        raw = json.dumps({"type": 1}).encode("utf-8")
        forged = other_key.sign(b"1700000000" + raw).hex()
        resp = _post_interaction(discord_key, {"type": 1}, signature=forged)
        assert resp.status_code == 401

    def test_garbage_signature_rejected(self, discord_key):
        resp = _post_interaction(discord_key, {"type": 1}, signature="zz-not-hex")
        assert resp.status_code == 401

    def test_missing_public_key_returns_503(self, settings):
        settings.DISCORD_PUBLIC_KEY = ""
        resp = Client().post(DISCORD_URL, data=b"{}", content_type="application/json")
        assert resp.status_code == 503

    def test_unknown_guild_gets_ephemeral_link_instructions(self, discord_key):
        with patch("apps.notifications.tasks.answer_chat_command.delay") as mock_delay:
            resp = _post_interaction(discord_key, _slash_payload("999888777"))
        assert resp.status_code == 200
        body = resp.json()
        assert body["type"] == 4
        assert body["data"]["flags"] == 64
        assert "999888777" in body["data"]["content"]
        mock_delay.assert_not_called()

    def test_known_guild_defers_and_enqueues(self, discord_key):
        connection = _connection("discord", "111222333")
        with patch("apps.notifications.tasks.answer_chat_command.delay") as mock_delay:
            resp = _post_interaction(discord_key, _slash_payload("111222333"))
        assert resp.status_code == 200
        assert resp.json() == {"type": 5}
        kwargs = mock_delay.call_args.kwargs
        assert kwargs["connection_id"] == str(connection.id)
        assert kwargs["command"] == "report"
        assert kwargs["respond_to"]["kind"] == "discord_followup"
        assert kwargs["respond_to"]["interaction_token"] == "interaction-token"

    def test_ask_subcommand_carries_question(self, discord_key):
        _connection("discord", "111222333")
        payload = _slash_payload(
            "111222333", "ask",
            sub_options=[{"name": "question", "type": 3, "value": "how are we doing"}],
        )
        with patch("apps.notifications.tasks.answer_chat_command.delay") as mock_delay:
            resp = _post_interaction(discord_key, payload)
        assert resp.json() == {"type": 5}
        kwargs = mock_delay.call_args.kwargs
        assert kwargs["command"] == "ask"
        assert kwargs["text"] == "how are we doing"

    def test_inactive_connection_not_resolved(self, discord_key):
        connection = _connection("discord", "111222333")
        connection.is_active = False
        connection.save(update_fields=["is_active"])
        with patch("apps.notifications.tasks.answer_chat_command.delay") as mock_delay:
            resp = _post_interaction(discord_key, _slash_payload("111222333"))
        assert resp.json()["type"] == 4
        mock_delay.assert_not_called()

    def test_unknown_interaction_type_gets_ephemeral(self, discord_key):
        resp = _post_interaction(discord_key, {"type": 99})
        body = resp.json()
        assert body["type"] == 4
        assert "Unsupported" in body["data"]["content"]

    def test_dm_without_guild_never_resolves_blank_team_connection(self, discord_key):
        # A webhook-only connection has a blank external_team_id. A DM
        # interaction carries no guild_id; it must be refused, never bound
        # to that (arbitrary) account.
        _connection("discord", "")
        with patch("apps.notifications.tasks.answer_chat_command.delay") as mock_delay:
            resp = _post_interaction(discord_key, _slash_payload(""))
        body = resp.json()
        assert body["type"] == 4
        assert "direct messages" in body["data"]["content"]
        mock_delay.assert_not_called()


# ── Slack signing helpers ─────────────────────────────────────────────────────


@pytest.fixture
def slack_secret(settings):
    settings.SLACK_SIGNING_SECRET = SIGNING_SECRET
    return SIGNING_SECRET


def _slack_signature(raw: bytes, timestamp: str, secret: str = SIGNING_SECRET) -> str:
    base = b"v0:" + timestamp.encode("utf-8") + b":" + raw
    return "v0=" + hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()


def _post_slack(url: str, raw: bytes, content_type: str, *,
                timestamp: str = "", signature: str = ""):
    timestamp = timestamp or str(int(time.time()))
    signature = signature or _slack_signature(raw, timestamp)
    return Client().post(
        url,
        data=raw,
        content_type=content_type,
        headers={
            "X-Slack-Request-Timestamp": timestamp,
            "X-Slack-Signature": signature,
        },
    )


# ── Slack events ──────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestSlackEvents:
    def test_url_verification_echoes_challenge(self, slack_secret):
        raw = json.dumps({"type": "url_verification", "challenge": "c-123"}).encode()
        resp = _post_slack(SLACK_EVENTS_URL, raw, "application/json")
        assert resp.status_code == 200
        assert resp.json() == {"challenge": "c-123"}

    def test_bad_signature_rejected(self, slack_secret):
        raw = json.dumps({"type": "url_verification", "challenge": "c"}).encode()
        resp = _post_slack(
            SLACK_EVENTS_URL, raw, "application/json",
            signature="v0=" + "0" * 64,
        )
        assert resp.status_code == 401

    def test_stale_timestamp_returns_200_without_enqueue(self, slack_secret):
        stale = str(int(time.time()) - 3600)
        raw = json.dumps(_mention_event("T123")).encode()
        with patch("apps.notifications.tasks.answer_chat_command.delay") as mock_delay:
            resp = _post_slack(
                SLACK_EVENTS_URL, raw, "application/json", timestamp=stale,
            )
        assert resp.status_code == 200
        assert resp.json()["reason"] == "stale_timestamp"
        mock_delay.assert_not_called()

    def test_missing_secret_returns_503(self, settings):
        settings.SLACK_SIGNING_SECRET = ""
        resp = Client().post(
            SLACK_EVENTS_URL, data=b"{}", content_type="application/json",
        )
        assert resp.status_code == 503

    def test_app_mention_enqueues_channel_reply(self, slack_secret):
        connection = _connection("slack", "T123")
        raw = json.dumps(_mention_event("T123")).encode()
        with patch("apps.notifications.tasks.answer_chat_command.delay") as mock_delay:
            resp = _post_slack(SLACK_EVENTS_URL, raw, "application/json")
        assert resp.status_code == 200
        kwargs = mock_delay.call_args.kwargs
        assert kwargs["connection_id"] == str(connection.id)
        assert kwargs["command"] == "report"  # leading <@BOT> stripped
        assert kwargs["respond_to"] == {
            "kind": "slack_channel", "channel": "C42", "thread_ts": "111.222",
        }

    def test_bot_messages_ignored(self, slack_secret):
        _connection("slack", "T123")
        event = _mention_event("T123")
        event["event"]["bot_id"] = "B999"
        raw = json.dumps(event).encode()
        with patch("apps.notifications.tasks.answer_chat_command.delay") as mock_delay:
            resp = _post_slack(SLACK_EVENTS_URL, raw, "application/json")
        assert resp.status_code == 200
        mock_delay.assert_not_called()

    def test_unknown_team_mention_is_dropped(self, slack_secret):
        raw = json.dumps(_mention_event("T-unknown")).encode()
        with patch("apps.notifications.tasks.answer_chat_command.delay") as mock_delay:
            resp = _post_slack(SLACK_EVENTS_URL, raw, "application/json")
        assert resp.status_code == 200
        mock_delay.assert_not_called()


def _mention_event(team_id: str) -> dict:
    return {
        "type": "event_callback",
        "team_id": team_id,
        "event": {
            "type": "app_mention",
            "user": "U777",
            "text": "<@U0BOTID> report",
            "channel": "C42",
            "ts": "111.222",
        },
    }


# ── Slack slash commands ──────────────────────────────────────────────────────


def _command_form(team_id: str, text: str) -> bytes:
    return urlencode({
        "team_id": team_id,
        "channel_id": "C42",
        "user_name": "tester",
        "text": text,
        "response_url": "https://hooks.slack.com/commands/T123/999/abc",
    }).encode("utf-8")


@pytest.mark.django_db
class TestSlackCommands:
    def test_known_team_acks_and_enqueues(self, slack_secret):
        connection = _connection("slack", "T123")
        raw = _command_form("T123", "security")
        with patch("apps.notifications.tasks.answer_chat_command.delay") as mock_delay:
            resp = _post_slack(
                SLACK_COMMANDS_URL, raw, "application/x-www-form-urlencoded",
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["response_type"] == "ephemeral"
        assert "thinking" in body["text"]
        kwargs = mock_delay.call_args.kwargs
        assert kwargs["connection_id"] == str(connection.id)
        assert kwargs["command"] == "security"
        assert kwargs["respond_to"] == {
            "kind": "slack_response_url",
            "url": "https://hooks.slack.com/commands/T123/999/abc",
        }

    def test_blank_team_id_never_resolves_blank_team_connection(self, slack_secret):
        _connection("slack", "")
        raw = _command_form("", "report")
        with patch("apps.notifications.tasks.answer_chat_command.delay") as mock_delay:
            resp = _post_slack(
                SLACK_COMMANDS_URL, raw, "application/x-www-form-urlencoded",
            )
        assert resp.status_code == 200
        assert resp.json()["response_type"] == "ephemeral"
        mock_delay.assert_not_called()

    def test_ask_command_splits_question(self, slack_secret):
        _connection("slack", "T123")
        raw = _command_form("T123", "ask how is my visibility")
        with patch("apps.notifications.tasks.answer_chat_command.delay") as mock_delay:
            _post_slack(SLACK_COMMANDS_URL, raw, "application/x-www-form-urlencoded")
        kwargs = mock_delay.call_args.kwargs
        assert kwargs["command"] == "ask"
        assert kwargs["text"] == "how is my visibility"

    def test_unknown_team_gets_link_instructions(self, slack_secret):
        raw = _command_form("T-unlinked", "report")
        with patch("apps.notifications.tasks.answer_chat_command.delay") as mock_delay:
            resp = _post_slack(
                SLACK_COMMANDS_URL, raw, "application/x-www-form-urlencoded",
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["response_type"] == "ephemeral"
        assert "T-unlinked" in body["text"]
        mock_delay.assert_not_called()

    def test_bad_signature_rejected(self, slack_secret):
        raw = _command_form("T123", "report")
        resp = _post_slack(
            SLACK_COMMANDS_URL, raw, "application/x-www-form-urlencoded",
            signature="v0=" + "f" * 64,
        )
        assert resp.status_code == 401


# ── Webhook rate limiter coverage ─────────────────────────────────────────────


class TestWebhookRateLimiter:
    def test_all_webhook_paths_are_rate_limited(self):
        from apps.billing.middleware.rate_limiter import WEBHOOK_PATHS

        assert WEBHOOK_PATHS == {
            "/api/v1/billing/polar/webhook/",
            DISCORD_URL,
            SLACK_EVENTS_URL,
            SLACK_COMMANDS_URL,
        }

    def test_bucket_names_derive_from_paths(self):
        from apps.billing.middleware.rate_limiter import WebhookRateLimitMiddleware

        middleware = WebhookRateLimitMiddleware(lambda request: None)
        assert middleware._bucket_name(
            "/api/v1/billing/polar/webhook/", "1.2.3.4",
        ) == "webhook:billing-polar-webhook:1.2.3.4"
        assert middleware._bucket_name(
            DISCORD_URL, "1.2.3.4",
        ) == "webhook:notifications-discord-interactions:1.2.3.4"

    def test_exhausted_bucket_returns_429(self):
        with patch(
            "apps.billing.middleware.rate_limiter.TokenBucket.try_acquire",
            return_value=False,
        ):
            resp = Client().post(
                DISCORD_URL, data=b"{}", content_type="application/json",
            )
        assert resp.status_code == 429

    def test_non_webhook_paths_bypass_the_limiter(self):
        with patch(
            "apps.billing.middleware.rate_limiter.TokenBucket.try_acquire",
            return_value=False,
        ):
            resp = Client().get("/api/v1/version/")
        assert resp.status_code == 200
