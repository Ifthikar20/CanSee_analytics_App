"""
Inbound chat-platform webhooks: Discord interactions + Slack events/commands.

Hardening:
    1. Raw-body signature verification before any parsing
       - Discord: Ed25519 over timestamp + body (X-Signature-Ed25519)
       - Slack:   HMAC-SHA256 over "v0:{ts}:{body}" (X-Slack-Signature)
    2. Replay protection (Slack timestamps older than 5 minutes rejected)
    3. All heavy work is deferred to the answer_chat_command Celery task;
       these views only verify, resolve the IntegrationConnection, and ack
       inside the platforms' 3-second response windows.

These are plain Django FBVs (csrf_exempt) - the platforms cannot send CSRF
tokens or JWTs. Rate limiting comes from WebhookRateLimitMiddleware.
"""
import hashlib
import hmac
import json
import logging
import time

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

logger = logging.getLogger("apps")

# Maximum age of a Slack request before we treat it as a replay.
SLACK_MAX_AGE_SECONDS = 300  # 5 minutes

# Discord interaction types / callback types we speak.
DISCORD_PING = 1
DISCORD_APPLICATION_COMMAND = 2
DISCORD_RESPONSE_PONG = 1
DISCORD_RESPONSE_MESSAGE = 4
DISCORD_RESPONSE_DEFERRED = 5
DISCORD_EPHEMERAL_FLAG = 64


# ── Discord ───────────────────────────────────────────────────────────────────


@csrf_exempt
@require_POST
def discord_interactions(request):
    """Discord Interactions endpoint (slash commands + URL verification PING)."""
    public_key = getattr(settings, "DISCORD_PUBLIC_KEY", "")
    if not public_key:
        logger.error("DISCORD_PUBLIC_KEY is not configured - cannot verify Discord interactions.")
        return HttpResponse(status=503)

    signature = request.headers.get("X-Signature-Ed25519", "")
    timestamp = request.headers.get("X-Signature-Timestamp", "")
    if not signature or not timestamp:
        logger.warning("Discord interaction rejected: missing signature headers.")
        return HttpResponse(status=401)

    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        verify_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key))
        verify_key.verify(
            bytes.fromhex(signature),
            timestamp.encode("utf-8") + request.body,
        )
    except (InvalidSignature, ValueError):
        logger.warning("Discord interaction rejected: invalid signature.")
        return HttpResponse(status=401)

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("Discord interaction rejected: unparseable body.")
        return HttpResponse(status=400)

    interaction_type = body.get("type")
    if interaction_type == DISCORD_PING:
        return JsonResponse({"type": DISCORD_RESPONSE_PONG})

    if interaction_type == DISCORD_APPLICATION_COMMAND:
        return _handle_discord_command(body)

    return _discord_ephemeral("Unsupported interaction.")


def _dispatch_answer(**kwargs):
    """Queue answer_chat_command for the origin platform.

    Under eager Celery, .delay() runs the task inline BEFORE the view
    returns its ack, and Discord rejects a follow-up that arrives ahead
    of the initial interaction response (404 Unknown Webhook, 10015).
    Dev sets CHAT_EAGER_THREAD_DISPATCH so the task runs in a short-lived
    thread after the ack has gone out; tests and production use the queue.
    """
    from apps.notifications.tasks import answer_chat_command

    if getattr(settings, "CHAT_EAGER_THREAD_DISPATCH", False):
        import threading

        from django.db import connections

        def _run() -> None:
            try:
                time.sleep(0.6)  # let the HTTP ack reach the platform first
                answer_chat_command.run(**kwargs)
            except Exception:
                logger.exception("chat answer thread failed")
            finally:
                connections.close_all()

        threading.Thread(target=_run, daemon=True, name="chat-answer").start()
        return
    answer_chat_command.delay(**kwargs)


def _discord_ephemeral(content: str) -> JsonResponse:
    return JsonResponse({
        "type": DISCORD_RESPONSE_MESSAGE,
        "data": {"content": content, "flags": DISCORD_EPHEMERAL_FLAG},
    })


def _handle_discord_command(body: dict) -> JsonResponse:
    data = body.get("data") or {}
    if data.get("name") != "cansee":
        return _discord_ephemeral("Unknown command.")

    guild_id = str(body.get("guild_id") or "")
    if not guild_id:
        # A DM has no guild id. Connections created webhook-only carry a
        # blank external_team_id, so an empty-id lookup would bind the
        # command to an arbitrary account - refuse instead.
        return _discord_ephemeral(
            "Cansee commands work inside a linked server, not in direct messages."
        )

    # /cansee <subcommand> [question]
    command = "help"
    text = ""
    options = data.get("options") or []
    if options:
        command = options[0].get("name") or "help"
        for opt in options[0].get("options") or []:
            if opt.get("name") == "question":
                text = str(opt.get("value") or "")

    from apps.notifications.models import IntegrationConnection

    connection = IntegrationConnection.objects.filter(
        platform="discord", is_active=True, external_team_id=guild_id,
    ).first()
    if connection is None:
        return _discord_ephemeral(
            "This Discord server is not linked to Cansee yet. Open Cansee "
            "Integrations settings, edit your Discord connection, and paste "
            f"this server id: {guild_id}"
        )

    member_user = (body.get("member") or {}).get("user") or body.get("user") or {}
    invoker = str(member_user.get("username") or "")

    _dispatch_answer(
        connection_id=str(connection.id),
        command=command,
        text=text,
        respond_to={
            "kind": "discord_followup",
            "interaction_token": str(body.get("token") or ""),
            "interaction_id": str(body.get("id") or ""),
            "channel_id": str(body.get("channel_id") or ""),
        },
        invoker=invoker,
    )
    # DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE: "Cansee is thinking..." -
    # the task follows up through the interaction webhook.
    return JsonResponse({"type": DISCORD_RESPONSE_DEFERRED})


# ── Slack ─────────────────────────────────────────────────────────────────────


def _verify_slack_request(request) -> tuple[bool, str]:
    """Verify the Slack signature over the RAW body.

    Returns (ok, failure_reason) where failure_reason is one of
    "" | "missing_secret" | "stale" | "bad".
    """
    signing_secret = getattr(settings, "SLACK_SIGNING_SECRET", "")
    if not signing_secret:
        return False, "missing_secret"

    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")
    if not timestamp or not signature:
        return False, "bad"

    try:
        request_ts = float(timestamp)
    except ValueError:
        return False, "bad"
    if abs(time.time() - request_ts) > SLACK_MAX_AGE_SECONDS:
        return False, "stale"

    base = b"v0:" + timestamp.encode("utf-8") + b":" + request.body
    expected = "v0=" + hmac.new(
        signing_secret.encode("utf-8"), base, hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return False, "bad"
    return True, ""


def _slack_signature_failure(reason: str):
    """Map a verification failure to the response Slack should see."""
    if reason == "missing_secret":
        logger.error("SLACK_SIGNING_SECRET is not configured - cannot verify Slack requests.")
        return HttpResponse(status=503)
    if reason == "stale":
        # 200 so Slack stops retrying a request we will never accept.
        logger.warning("Slack request rejected: stale timestamp (possible replay).")
        return JsonResponse({"status": "rejected", "reason": "stale_timestamp"}, status=200)
    logger.warning("Slack request rejected: invalid signature.")
    return HttpResponse(status=401)


def _resolve_slack_connection(team_id: str):
    from apps.notifications.models import IntegrationConnection

    team_id = str(team_id or "").strip()
    if not team_id:
        # Webhook-only connections have a blank external_team_id; an empty
        # lookup key must never resolve to one of them.
        return None
    return IntegrationConnection.objects.filter(
        platform="slack", is_active=True, external_team_id=team_id,
    ).first()


def _split_command(text: str) -> tuple[str, str]:
    """First token is the command, the rest is its argument text."""
    parts = (text or "").strip().split(None, 1)
    if not parts:
        return "help", ""
    return parts[0].lower(), parts[1] if len(parts) > 1 else ""


@csrf_exempt
@require_POST
def slack_events(request):
    """Slack Events API endpoint (url_verification + app_mention)."""
    ok, reason = _verify_slack_request(request)
    if not ok:
        return _slack_signature_failure(reason)

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("Slack event rejected: unparseable body.")
        return HttpResponse(status=400)

    if body.get("type") == "url_verification":
        return JsonResponse({"challenge": body.get("challenge", "")})

    if body.get("type") == "event_callback":
        event = body.get("event") or {}

        # Loop guard: never react to bot messages (including our own).
        if event.get("bot_id") or event.get("subtype") == "bot_message":
            return JsonResponse({"status": "ignored"})

        if event.get("type") == "app_mention":
            _handle_slack_mention(body, event)

    # Always 200 fast - Slack retries anything else.
    return JsonResponse({"status": "ok"})


def _handle_slack_mention(body: dict, event: dict) -> None:
    connection = _resolve_slack_connection(body.get("team_id", ""))
    if connection is None:
        logger.info(
            f"Slack mention from unlinked team {body.get('team_id', '')} ignored. "
            "Link the workspace in Cansee Integrations settings."
        )
        return

    # "<@U012BOT> ask how are we doing" -> "ask how are we doing"
    text = (event.get("text") or "").strip()
    if text.startswith("<@") and ">" in text:
        text = text.split(">", 1)[1].strip()
    command, arg_text = _split_command(text)

    _dispatch_answer(
        connection_id=str(connection.id),
        command=command,
        text=arg_text,
        respond_to={
            "kind": "slack_channel",
            "channel": event.get("channel", ""),
            "thread_ts": event.get("thread_ts") or event.get("ts", ""),
        },
        invoker=str(event.get("user") or ""),
    )


@csrf_exempt
@require_POST
def slack_commands(request):
    """Slack slash-command endpoint (/cansee <command> [text])."""
    # Verify against the RAW body bytes before touching the parsed form.
    ok, reason = _verify_slack_request(request)
    if not ok:
        return _slack_signature_failure(reason)

    form = request.POST
    team_id = form.get("team_id", "")
    response_url = form.get("response_url", "")
    command, arg_text = _split_command(form.get("text", ""))

    connection = _resolve_slack_connection(team_id)
    if connection is None:
        return JsonResponse({
            "response_type": "ephemeral",
            "text": (
                "This Slack workspace is not linked to Cansee yet. Open "
                "Cansee Integrations settings, edit your Slack connection, "
                f"and paste this team id: {team_id}"
            ),
        })

    _dispatch_answer(
        connection_id=str(connection.id),
        command=command,
        text=arg_text,
        respond_to={"kind": "slack_response_url", "url": response_url},
        invoker=form.get("user_name", ""),
    )
    return JsonResponse({
        "response_type": "ephemeral",
        "text": "Cansee is thinking - answer coming up.",
    })
