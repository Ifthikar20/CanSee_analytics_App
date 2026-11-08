"""Versioning + lifecycle helpers for BrandFact rows.

Mutating a fact never overwrites the row in place. ``supersede_fact``
sets ``version_to=now`` on the old row, links a freshly-created row via
``superseded_by``, and writes a FactRevision so the audit trail is
preserved. ``approve_fact`` and ``reject_fact`` flip the lifecycle state
and similarly emit a revision row.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from apps.brand_vault.models import BrandFact, FactRevision, FactStatus


def _snapshot(fact: BrandFact) -> dict:
    return {
        "subject": fact.subject,
        "predicate": fact.predicate,
        "object": fact.object,
        "status": fact.status,
        "confidence": fact.confidence,
        "version_from": fact.version_from.isoformat() if fact.version_from else None,
        "version_to": fact.version_to.isoformat() if fact.version_to else None,
    }


@transaction.atomic
def supersede_fact(
    old_fact_id: str,
    new_subject: str,
    new_predicate: str,
    new_object: str,
    *,
    actor_user=None,
) -> BrandFact:
    """Mark old fact superseded and create a replacement linked back."""
    old = BrandFact.objects.select_for_update().get(id=old_fact_id)
    before = _snapshot(old)

    now = timezone.now()
    new_fact = BrandFact.objects.create(
        website=old.website,
        subject=new_subject,
        predicate=new_predicate,
        object=new_object,
        source_chunk=old.source_chunk,
        source_url=old.source_url,
        extracted_by="manual",
        confidence=1.0,
        status=FactStatus.APPROVED,
        version_from=now,
        product_line=old.product_line,
        topic=old.topic,
        audience=old.audience,
    )

    # Embed the new triple. No vector when embeddings are unavailable;
    # refresh_fact_embeddings fills it in later.
    from apps.brand_vault.services.embeddings import embed_text

    vector, _model = embed_text(
        f"{new_subject} {new_predicate} {new_object}",
        user=actor_user or getattr(old.website, "user", None),
        website=old.website,
    )
    if vector:
        new_fact.embedding = vector
        new_fact.save(update_fields=["embedding", "updated_at"])

    old.version_to = now
    old.superseded_by = new_fact
    old.save(update_fields=["version_to", "superseded_by", "updated_at"])

    FactRevision.objects.create(
        fact=old,
        action=FactRevision.ACTION_SUPERSEDED,
        before=before,
        after=_snapshot(new_fact),
        actor_user=actor_user,
    )
    FactRevision.objects.create(
        fact=new_fact,
        action=FactRevision.ACTION_CREATED,
        before={},
        after=_snapshot(new_fact),
        actor_user=actor_user,
    )
    return new_fact


@transaction.atomic
def approve_fact(fact_id: str, *, actor_user=None) -> BrandFact:
    fact = BrandFact.objects.select_for_update().get(id=fact_id)
    before = _snapshot(fact)
    fact.status = FactStatus.APPROVED
    fact.save(update_fields=["status", "updated_at"])
    FactRevision.objects.create(
        fact=fact,
        action=FactRevision.ACTION_APPROVED,
        before=before,
        after=_snapshot(fact),
        actor_user=actor_user,
    )
    return fact


@transaction.atomic
def reject_fact(fact_id: str, *, actor_user=None) -> BrandFact:
    fact = BrandFact.objects.select_for_update().get(id=fact_id)
    before = _snapshot(fact)
    fact.status = FactStatus.REJECTED
    fact.version_to = timezone.now()
    fact.save(update_fields=["status", "version_to", "updated_at"])
    FactRevision.objects.create(
        fact=fact,
        action=FactRevision.ACTION_REJECTED,
        before=before,
        after=_snapshot(fact),
        actor_user=actor_user,
    )
    return fact


def record_creation(fact: BrandFact, *, actor_user=None) -> FactRevision | None:
    return FactRevision.objects.create(
        fact=fact,
        action=FactRevision.ACTION_CREATED,
        before={},
        after=_snapshot(fact),
        actor_user=actor_user,
    )
