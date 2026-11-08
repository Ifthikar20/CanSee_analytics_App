"""Link a Slack workspace or Discord server to one Cansee connection.

The workspace id used to be typed into Cansee by hand, so any tenant could
claim another tenant's workspace and receive its commands. A link now
needs both sides to agree:

1. The Cansee user creates a short-lived link code for their connection
   (``create_link_code``). The code is shown once and kept only as a hash.
2. Someone in the workspace runs ``/cansee link <code>``. Slack or Discord
   signs that request, so the team or guild id in it is genuine
   (``link_workspace``).

Only then is ``external_team_id`` set and marked verified. A workspace
already verified for another active connection is never taken over.
"""
from __future__ import annotations

import hashlib
import secrets

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone

#: How long a link code works, in seconds.
LINK_CODE_TTL_SECONDS = 15 * 60
#: Failed link attempts allowed per workspace in LINK_FAILURE_WINDOW_SECONDS.
LINK_MAX_FAILURES = 10
LINK_FAILURE_WINDOW_SECONDS = 60 * 60
#: Unambiguous characters (no 0/O, 1/I/L) for codes people type.
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 10

LINKABLE_PLATFORMS = ("slack", "discord")

PLATFORM_NAMES = {"slack": "Slack workspace", "discord": "Discord server"}


def _normalize(code: str) -> str:
    return "".join(ch for ch in (code or "").upper() if ch.isalnum())


def _code_key(platform: str, code: str) -> str:
    digest = hashlib.sha256(_normalize(code).encode("utf-8")).hexdigest()
    return f"chat_link_code:{platform}:{digest}"


def _failures_key(platform: str, team_id: str) -> str:
    return f"chat_link_failures:{platform}:{team_id}"


def create_link_code(connection) -> dict:
    """Mint a one-time code for ``connection`` and return how to use it."""
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(_CODE_LENGTH))
    code = f"{raw[:5]}-{raw[5:]}"
    cache.set(_code_key(connection.platform, code), str(connection.id),
              timeout=LINK_CODE_TTL_SECONDS)
    return {
        "code": code,
        "command": f"/cansee link {code}",
        "expires_in": LINK_CODE_TTL_SECONDS,
    }


def link_workspace(*, platform: str, team_id: str, code: str) -> str:
    """Bind the workspace that sent a signed ``link`` command. Returns the
    reply to show in the chat."""
    from apps.notifications.models import IntegrationConnection

    place = PLATFORM_NAMES.get(platform, "workspace")
    team_id = str(team_id or "").strip()
    if not team_id:
        return f"Run this command inside the {place} you want to link."

    failures_key = _failures_key(platform, team_id)
    if (cache.get(failures_key) or 0) >= LINK_MAX_FAILURES:
        return "Too many link attempts from here. Wait up to an hour, then try a new code."

    key = _code_key(platform, code)
    connection_id = cache.get(key) if _normalize(code) else None
    connection = (
        IntegrationConnection.objects.filter(id=connection_id, platform=platform).first()
        if connection_id else None
    )
    if connection is None:
        cache.add(failures_key, 0, timeout=LINK_FAILURE_WINDOW_SECONDS)
        try:
            cache.incr(failures_key)
        except ValueError:
            cache.set(failures_key, 1, timeout=LINK_FAILURE_WINDOW_SECONDS)
        return (
            "That link code is not valid or has expired. In Cansee Integrations "
            "settings, create a new link code and run the command it shows."
        )

    holder = (
        IntegrationConnection.objects.filter(
            platform=platform, external_team_id=team_id,
            external_team_verified_at__isnull=False, is_active=True,
        ).exclude(id=connection.id).first()
    )
    if holder is not None:
        return (
            f"This {place} is already linked to another Cansee account. "
            "That account has to disconnect it before it can be linked here."
        )

    try:
        with transaction.atomic():
            # Release the id from any other row: unverified claims typed in
            # by hand, and inactive connections.
            (IntegrationConnection.objects
             .filter(platform=platform, external_team_id=team_id)
             .exclude(id=connection.id)
             .update(external_team_id="", external_team_verified_at=None))
            connection.external_team_id = team_id
            connection.external_team_verified_at = timezone.now()
            connection.save(update_fields=[
                "external_team_id", "external_team_verified_at", "updated_at",
            ])
    except IntegrityError:
        return (
            f"This {place} is already linked to another Cansee account. "
            "That account has to disconnect it before it can be linked here."
        )

    cache.delete(key)  # single use
    cache.delete(failures_key)
    return f"This {place} is now linked to Cansee. Run /cansee help to see what you can ask."


def resolve_connection(*, platform: str, team_id: str):
    """The one active connection verified for this workspace, or None."""
    from apps.notifications.models import IntegrationConnection

    team_id = str(team_id or "").strip()
    if not team_id:
        # Webhook-only connections have a blank external_team_id; an empty
        # lookup key must never resolve to one of them.
        return None
    return IntegrationConnection.objects.filter(
        platform=platform, is_active=True, external_team_id=team_id,
        external_team_verified_at__isnull=False,
    ).first()
