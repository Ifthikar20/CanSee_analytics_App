"""Who may change a project's agent work, and who decides its reviews.

Reads go through WebsiteService.get_for_user (viewers included); these
rules sit on top of it for writes and review decisions.
"""
from __future__ import annotations

from core.permissions.rbac import role_at_least


def _org_role(user, website) -> str | None:
    """The user's role in the website's own organization, or None. A
    membership in another organization never counts."""
    if not website.organization_id:
        return None
    return (
        user.org_memberships.filter(organization_id=website.organization_id)
        .values_list("role", flat=True)
        .first()
    )


def can_write(user, website) -> bool:
    """The website's owner, or a member (or above) of its organization."""
    if website.user_id == user.id:
        return True
    role = _org_role(user, website)
    return bool(role) and role_at_least(role, "member")


def is_project_admin(user, website) -> bool:
    """The website's owner, or an admin or owner of its organization."""
    if website.user_id == user.id:
        return True
    role = _org_role(user, website)
    return bool(role) and role_at_least(role, "admin")
