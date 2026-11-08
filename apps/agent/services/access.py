"""Who may change a project's agent work, and who decides its reviews.

Reads go through WebsiteService.get_for_user (viewers included); these
rules sit on top of it for writes and review decisions. Both go by the
user's current role (WebsiteService.role_for): on an org project that is
their membership role, re-read every time, so creating the project grants
nothing once they are removed or downgraded.
"""
from __future__ import annotations

from core.permissions.rbac import role_at_least


def _role(user, website) -> str | None:
    from apps.websites.services.website_service import WebsiteService

    return WebsiteService.role_for(user, website)


def can_write(user, website) -> bool:
    """The owner of a personal project, or a member (or above) of the
    project's organization."""
    role = _role(user, website)
    return bool(role) and role_at_least(role, "member")


def is_project_admin(user, website) -> bool:
    """The owner of a personal project, or an admin or owner of the
    project's organization."""
    role = _role(user, website)
    return bool(role) and role_at_least(role, "admin")
