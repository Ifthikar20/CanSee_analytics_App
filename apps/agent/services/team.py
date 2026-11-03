"""A project's team: the people who can write its agent work and review it.

The website's owner plus the members of its organization with role
member or above, the rule WebsiteService.get_for_user applies to writes.
Inactive accounts are left out.
"""
from __future__ import annotations

from core.permissions.rbac import role_at_least


def _name(user) -> str:
    return (getattr(user, "full_name", "") or "").strip() or user.email


def team_for(website) -> list[dict]:
    """``[{id, name, email, role, user}]``: the owner first, then the
    organization's writers by name."""
    from apps.accounts.models import OrganizationMember

    members = []
    if website.organization_id:
        members = list(
            OrganizationMember.objects.filter(
                organization_id=website.organization_id, user__is_active=True,
            ).select_related("user")
        )
    roles = {m.user_id: m.role for m in members}
    team = []
    owner = website.user
    if owner is not None and owner.is_active:
        team.append({"id": owner.id, "name": _name(owner), "email": owner.email,
                     "role": roles.get(owner.id) or "owner", "user": owner})
    others = sorted(
        (m for m in members if m.user_id != website.user_id and role_at_least(m.role, "member")),
        key=lambda m: (_name(m.user).lower(), m.user.email),
    )
    team += [{"id": m.user_id, "name": _name(m.user), "email": m.user.email, "role": m.role,
              "user": m.user} for m in others]
    return team


def team_ids(website) -> set:
    return {member["id"] for member in team_for(website)}
