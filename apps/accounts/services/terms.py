"""Terms of Service acceptance (clickwrap).

``settings.TERMS_VERSION`` names the current Terms (the date it took
effect). A user must accept it before using the app; when the Terms change,
bumping the version asks everyone to accept again. The session payload
carries ``terms_state`` and the UI blocks the app until it is accepted.
"""
from __future__ import annotations

from django.conf import settings
from django.utils import timezone


class TermsVersionMismatch(Exception):
    """The version the user accepted is not the current one."""


def current_version() -> str:
    return str(getattr(settings, "TERMS_VERSION", "") or "")


def terms_state(user) -> dict:
    """``{current_version, accepted_version, accepted_at, needs_acceptance}``."""
    current = current_version()
    accepted = getattr(user, "terms_accepted_version", "") or ""
    accepted_at = getattr(user, "terms_accepted_at", None)
    return {
        "current_version": current,
        "accepted_version": accepted or None,
        "accepted_at": accepted_at.isoformat() if accepted_at else None,
        "needs_acceptance": bool(current) and accepted != current,
    }


def accept_terms(user, *, version, ip_address: str = "") -> dict:
    """Record acceptance of the current version; raise TermsVersionMismatch
    for any other version. Accepting again is harmless."""
    current = current_version()
    if not current or version != current:
        raise TermsVersionMismatch(version)
    user.terms_accepted_version = current
    user.terms_accepted_at = timezone.now()
    user.terms_accepted_ip = ip_address or None
    user.save(update_fields=["terms_accepted_version", "terms_accepted_at", "terms_accepted_ip"])
    return terms_state(user)
