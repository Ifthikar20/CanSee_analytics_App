"""django-axes hooks.

Lockouts are keyed on the email the user typed. Without normalising it,
"Bob@x.com" and "bob@x.com" counted as different usernames, so changing
the letter case bought five fresh attempts.
"""
from __future__ import annotations


def lockout_username(request, credentials=None) -> str | None:
    """The lockout key for a sign-in attempt: the email, trimmed and in
    lowercase (AXES_USERNAME_CALLABLE)."""
    value = None
    if credentials:
        value = credentials.get("email") or credentials.get("username")
    if value is None and request is not None:
        data = getattr(request, "data", None) or getattr(request, "POST", None) or {}
        try:
            value = data.get("email") or data.get("username")
        except AttributeError:
            value = None
    if not isinstance(value, str):
        return None
    return value.strip().lower() or None
