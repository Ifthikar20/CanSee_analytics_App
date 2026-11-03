"""Per-website brand context the agents write with (audience, voice,
offerings, rules).

Shared by the website's members and edited with optimistic concurrency:
every save names the revision it was based on, and a save based on an
older revision is refused with 409 revision_conflict instead of silently
overwriting a teammate's edit.
"""
from __future__ import annotations

from django.db import IntegrityError, transaction

from apps.agent.exceptions import RevisionConflict
from apps.agent.models import AgentBrandContext


def context_for(website) -> AgentBrandContext:
    """The saved context, or an empty unsaved one at revision 0."""
    context = (
        AgentBrandContext.objects.select_related("updated_by").filter(website=website).first()
    )
    return context or AgentBrandContext(website=website)


def save_context(
    website,
    user,
    *,
    revision: int,
    audience: str = "",
    voice: str = "",
    offerings: list | None = None,
    rules: list | None = None,
) -> AgentBrandContext:
    """Replace the context; ``revision`` must be the one the edit started from."""
    values = {
        "audience": audience,
        "voice": voice,
        "offerings": offerings or [],
        "rules": rules or [],
        "updated_by": user,
    }
    with transaction.atomic():
        context = AgentBrandContext.objects.select_for_update().filter(website=website).first()
        if context is None:
            if revision != 0:
                raise RevisionConflict(revision=0)
            try:
                with transaction.atomic():
                    context = AgentBrandContext.objects.create(website=website, revision=1, **values)
            except IntegrityError:
                # A teammate created it first.
                current = AgentBrandContext.objects.filter(website=website).values_list(
                    "revision", flat=True,
                ).first()
                raise RevisionConflict(revision=current) from None
            return context
        if context.revision != revision:
            raise RevisionConflict(revision=context.revision)
        for field, value in values.items():
            setattr(context, field, value)
        context.revision = revision + 1
        context.save()
    return context
