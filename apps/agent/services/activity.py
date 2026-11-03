"""The activity feed: the user's finished runs and new artifact versions in
the website, newest first.

Runs are private (only the user's own appear). Artifact versions are shared
with every member; their chat link is only given to the chat's owner.
"""
from __future__ import annotations

from apps.agent.constants import RunStatus
from apps.agent.models import AgentArtifactVersion, AgentRun

ACTIVITY_LIMIT = 20


def activity_for(user, website) -> list[dict]:
    runs = (
        AgentRun.objects.filter(
            user=user, website=website, status=RunStatus.COMPLETED, finished_at__isnull=False,
        )
        .order_by("-finished_at")
        .values("finished_at", "chat_id", "chat__title")[:ACTIVITY_LIMIT]
    )
    versions = (
        AgentArtifactVersion.objects.filter(
            artifact__website=website, artifact__deleted_at__isnull=True,
        )
        .order_by("-created_at")
        .values(
            "created_at", "version", "artifact_id", "artifact__title", "artifact__kind",
            "artifact__source_chat_id", "artifact__source_chat__user_id",
        )[:ACTIVITY_LIMIT]
    )
    items = [
        {
            "type": "run_completed",
            "at": run["finished_at"],
            "chat_id": run["chat_id"],
            "title": run["chat__title"],
        }
        for run in runs
    ]
    items += [
        {
            "type": "artifact_version",
            "at": v["created_at"],
            "chat_id": (
                v["artifact__source_chat_id"]
                if v["artifact__source_chat__user_id"] == user.id
                else None
            ),
            "title": v["artifact__title"],
            "artifact_id": v["artifact_id"],
            "kind": v["artifact__kind"],
            "version": v["version"],
        }
        for v in versions
    ]
    items.sort(key=lambda item: item["at"], reverse=True)
    return items[:ACTIVITY_LIMIT]
