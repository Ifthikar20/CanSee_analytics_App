"""Artifacts: pages, documents, sheets and reports.

Shared with every member of the website, so lookups filter on the website
only. Versions are immutable and numbered from 1; ``add_version`` is the
one writer, for sub-agents and saved reports alike. Deleting is soft.
"""
from __future__ import annotations

import hashlib
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.agent.constants import (
    ARTIFACT_SUMMARY_MAX_LENGTH,
    ARTIFACT_TITLE_MAX_LENGTH,
    CONTENT_TYPE_BY_KIND,
    VersionSource,
)
from apps.agent.models import AgentArtifact, AgentArtifactVersion, AgentMessage
from core.exceptions import ResourceNotFound

LIST_LIMIT = 200


def artifacts_for(website):
    """Live (not deleted) artifacts of ``website``."""
    return AgentArtifact.objects.filter(website=website, deleted_at__isnull=True)


def list_artifacts(website, *, kinds: list[str] | None = None, q: str = "") -> list[AgentArtifact]:
    """Newest first. Artifacts that never got a version are left out."""
    qs = (
        artifacts_for(website)
        .filter(latest_version__gt=0)
        .select_related("created_by", "source_chat")
        .order_by("-updated_at", "-id")
    )
    if kinds:
        qs = qs.filter(kind__in=kinds)
    q = (q or "").strip()[:ARTIFACT_TITLE_MAX_LENGTH]
    if q:
        qs = qs.filter(title__icontains=q)
    return list(qs[:LIST_LIMIT])


def get_artifact(website, artifact_id) -> AgentArtifact:
    artifact = (
        artifacts_for(website)
        .select_related("created_by", "source_chat")
        .filter(pk=artifact_id)
        .first()
    )
    if artifact is None:
        raise ResourceNotFound("Artifact not found.")
    return artifact


def latest_version(artifact: AgentArtifact) -> AgentArtifactVersion | None:
    if not artifact.latest_version:
        return None
    return artifact.versions.filter(version=artifact.latest_version).first()


def get_version(artifact: AgentArtifact, number: int) -> AgentArtifactVersion:
    version = artifact.versions.select_related("created_by").filter(version=number).first()
    if version is None:
        raise ResourceNotFound("Version not found.")
    return version


def list_versions(artifact: AgentArtifact) -> list[AgentArtifactVersion]:
    """Newest first, without the content."""
    return list(artifact.versions.defer("content", "data").order_by("-version"))


def rename_artifact(artifact: AgentArtifact, title: str) -> AgentArtifact:
    artifact.title = title.strip()[:ARTIFACT_TITLE_MAX_LENGTH]
    artifact.save(update_fields=["title", "updated_at"])
    return artifact


def delete_artifact(artifact: AgentArtifact) -> None:
    """Soft delete. A deleted report frees its reply to be saved again."""
    with transaction.atomic():
        artifact.deleted_at = timezone.now()
        artifact.save(update_fields=["deleted_at", "updated_at"])
        AgentMessage.objects.filter(saved_report=artifact).update(saved_report=None)


def add_version(
    artifact: AgentArtifact,
    *,
    content: str,
    content_type: str | None = None,
    data=None,
    summary: str = "",
    warnings: list | None = None,
    source: str = VersionSource.AGENT,
    created_by=None,
    run=None,
    cost_usd=Decimal("0"),
) -> AgentArtifactVersion:
    """Append version ``latest_version + 1`` and move the artifact to it.

    The artifact row is locked for the numbering, so concurrent writers
    get consecutive versions instead of a unique-constraint error.
    """
    content = _strip_nul(content or "")
    encoded = content.encode("utf-8")
    with transaction.atomic():
        locked = AgentArtifact.objects.select_for_update().get(pk=artifact.pk)
        number = locked.latest_version + 1
        version = AgentArtifactVersion.objects.create(
            artifact=locked,
            version=number,
            content=content,
            content_type=content_type or CONTENT_TYPE_BY_KIND[locked.kind],
            data=_strip_nul_deep(data),
            sha256=hashlib.sha256(encoded).hexdigest(),
            size_bytes=len(encoded),
            summary=_strip_nul(summary or "")[:ARTIFACT_SUMMARY_MAX_LENGTH],
            warnings=_strip_nul_deep(warnings or []),
            source=source,
            created_by=created_by,
            run=run,
            cost_usd=cost_usd,
        )
        locked.latest_version = number
        locked.save(update_fields=["latest_version", "updated_at"])
    artifact.latest_version = number
    artifact.updated_at = locked.updated_at
    return version


def _strip_nul(text: str) -> str:
    # Postgres text and JSONB both reject NUL.
    return text.replace("\x00", "")


def _strip_nul_deep(value):
    if isinstance(value, str):
        return _strip_nul(value)
    if isinstance(value, list):
        return [_strip_nul_deep(v) for v in value]
    if isinstance(value, dict):
        return {_strip_nul(str(k)): _strip_nul_deep(v) for k, v in value.items()}
    return value
