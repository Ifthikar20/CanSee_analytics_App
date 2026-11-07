"""Findings: a project's list of things to fix (swarm_contract.md 6a, 6d).

Findings are shared with the website's members: viewers read, members and
above write (the views check that). A finding comes from a row of a
research table (its draft is copied, and the row's sha1 ``dedupe_key``
keeps one finding per row) or is written by hand. The chat a finding came
from stays private: only that chat's owner gets its ids.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import uuid
from datetime import datetime

from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.agent.services.runs import InvalidRequest
from apps.agent.tools.shaping import clip
from core.exceptions import CanseeException, ResourceNotFound

FINDINGS_PAGE_SIZE = 50
MAX_ROWS_PER_CREATE = 50
SEARCH_MAX_LENGTH = 200
TITLE_MAX_LENGTH = 200
DETAIL_MAX_LENGTH = 2000
PROMPT_TEXT_MAX_LENGTH = 300
MAX_EVIDENCE = 10
EVIDENCE_LABEL_MAX_LENGTH = 200
EVIDENCE_URL_MAX_LENGTH = 2000
EVIDENCE_KINDS = ("serp", "page", "link", "answer")
SHEETS = ("prompts", "gaps", "sources")
STATUS_FILTER_ALL = "all"


class InvalidCursor(CanseeException):
    def __init__(self):
        super().__init__(
            "The findings list position is not valid. Load the list again.",
            code="invalid", status_code=400,
        )


def findings_for(website):
    from apps.agent.models import AgentFinding

    return AgentFinding.objects.filter(website=website)


def dedupe_key(swarm_id, sheet: str, row_key: str) -> str:
    """sha1 hex of "{swarm_id}:{sheet}:{row_key}": one finding per table row."""
    return hashlib.sha1(f"{swarm_id}:{sheet}:{row_key}".encode()).hexdigest()


# ── Listing ───────────────────────────────────────────────────────────────


def status_counts(website) -> dict:
    """{open, done, dismissed} over every finding of the website."""
    from apps.agent.constants import FindingStatus

    totals = dict(
        findings_for(website).order_by().values_list("status").annotate(n=Count("id"))
    )
    return {status: totals.get(status, 0) for status in FindingStatus.values}


def list_findings(website, *, status: str = "open", category: str | None = None,
                  severity: str | None = None, swarm_id=None, q: str = "",
                  cursor: str = "") -> tuple[list, str | None, dict]:
    """(findings, next_cursor, counts): newest first, FINDINGS_PAGE_SIZE
    per page, keyset-paginated. ``status`` "all" lists every status; ``q``
    matches the title, the detail and the prompt. ``counts`` ignore every
    filter."""
    qs = (findings_for(website)
          .select_related("source_swarm", "source_chat", "created_by", "status_changed_by")
          .order_by("-created_at", "-id"))
    if status and status != STATUS_FILTER_ALL:
        qs = qs.filter(status=status)
    if category:
        qs = qs.filter(category=category)
    if severity:
        qs = qs.filter(severity=severity)
    if swarm_id:
        qs = qs.filter(source_swarm_id=swarm_id)
    q = (q or "").strip()[:SEARCH_MAX_LENGTH]
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(detail__icontains=q)
                       | Q(prompt_text__icontains=q))
    if cursor:
        at, last_id = _decode_cursor(cursor)
        qs = qs.filter(Q(created_at__lt=at) | Q(created_at=at, id__lt=last_id))
    page = list(qs[: FINDINGS_PAGE_SIZE + 1])
    next_cursor = None
    if len(page) > FINDINGS_PAGE_SIZE:
        page = page[:FINDINGS_PAGE_SIZE]
        next_cursor = _encode_cursor(page[-1].created_at, page[-1].id)
    return page, next_cursor, status_counts(website)


def get_finding(website, finding_id):
    finding = (findings_for(website)
               .select_related("source_swarm", "source_chat", "created_by", "status_changed_by")
               .filter(id=finding_id).first())
    if finding is None:
        raise ResourceNotFound("Finding not found.")
    return finding


# ── Creating ──────────────────────────────────────────────────────────────


def _clean_evidence(items) -> list[dict]:
    """Evidence as the contract shapes it: known kinds, http(s) links only,
    at most MAX_EVIDENCE items."""
    out = []
    for item in items or []:
        if not isinstance(item, dict) or item.get("kind") not in EVIDENCE_KINDS:
            continue
        url = item.get("url")
        url = url if isinstance(url, str) and url.lower().startswith(("http://", "https://")) \
            else None
        tab_id = item.get("tab_id")
        tab_id = tab_id if isinstance(tab_id, str) and tab_id else None
        if item["kind"] == "link" and url is None:
            continue
        out.append({
            "kind": item["kind"],
            "label": clip(item.get("label"), EVIDENCE_LABEL_MAX_LENGTH),
            "tab_id": tab_id,
            "url": url[:EVIDENCE_URL_MAX_LENGTH] if url else None,
        })
        if len(out) == MAX_EVIDENCE:
            break
    return out


def _draft_fields(draft: dict) -> dict:
    from apps.agent.constants import FINDING_CATEGORIES, FindingSeverity

    category = draft.get("category")
    severity = draft.get("severity")
    return {
        "title": clip(draft.get("title"), TITLE_MAX_LENGTH),
        "detail": clip(draft.get("detail"), DETAIL_MAX_LENGTH),
        "category": category if category in FINDING_CATEGORIES else "other",
        "severity": severity if severity in FindingSeverity.values else FindingSeverity.MEDIUM,
        "prompt_text": clip(draft.get("prompt_text"), PROMPT_TEXT_MAX_LENGTH),
        "evidence": _clean_evidence(draft.get("evidence")),
    }


def create_from_swarm(user, website, *, swarm_id, version: int, sheet: str,
                      row_keys: list[str]) -> tuple[list, list]:
    """(created, duplicates) for rows of one version's table. The research
    must be one of the user's chats in this website and the version must
    have a table (404 otherwise); every row must exist in that sheet (400
    invalid) and have a finding draft (400 no_finding, nothing created). A
    row already in Findings is returned under ``duplicates``."""
    from apps.agent.exceptions import NoFinding
    from apps.agent.models import AgentFinding, AgentSwarm, AgentSwarmVersion

    swarm = (AgentSwarm.objects.select_related("chat")
             .filter(id=swarm_id, website=website, chat__user=user).first())
    if swarm is None:
        raise ResourceNotFound("Research not found.")
    row = (AgentSwarmVersion.objects.filter(swarm=swarm, version=version)
           .exclude(table__isnull=True).first())
    if row is None:
        raise ResourceNotFound("This version of the research has no results table.")
    sheets = {s.get("key"): s for s in (row.table or {}).get("sheets") or []
              if isinstance(s, dict)}
    rows = {r.get("key"): r for r in (sheets.get(sheet) or {}).get("rows") or []
            if isinstance(r, dict)}
    keys = list(dict.fromkeys(str(k) for k in row_keys))
    unknown = [k for k in keys if k not in rows]
    if unknown:
        raise InvalidRequest(f"These rows are not in the {sheet} sheet of version "
                             f"{version}: {', '.join(unknown[:5])}.")
    without = [k for k in keys if not isinstance(rows[k].get("finding"), dict)]
    if without:
        raise NoFinding(without)

    created, duplicates = [], []
    with transaction.atomic():
        for key in keys:
            digest = dedupe_key(swarm.id, sheet, key)
            existing = AgentFinding.objects.filter(website=website, dedupe_key=digest).first()
            if existing is not None:
                duplicates.append(existing)
                continue
            fields = _draft_fields(rows[key]["finding"])
            try:
                with transaction.atomic():
                    finding = AgentFinding.objects.create(
                        website=website, **fields,
                        source_swarm=swarm, source_version=row.version, source_sheet=sheet,
                        source_row_key=key,
                        source_task_ids=[str(t) for t in rows[key].get("task_ids") or []],
                        source_chat=swarm.chat, source_message_id=row.message_id,
                        dedupe_key=digest, created_by=user,
                    )
            except IntegrityError:
                # Added by a concurrent request for the same row.
                duplicates.append(AgentFinding.objects.get(website=website, dedupe_key=digest))
                continue
            created.append(finding)
    return _reloaded(created), _reloaded(duplicates)


def create_manual(user, website, *, title: str, category: str, severity: str,
                  detail: str = "", prompt_text: str = ""):
    """A finding written by hand (no source, no dedupe key)."""
    from apps.agent.models import AgentFinding

    finding = AgentFinding.objects.create(
        website=website, title=clip(" ".join(title.split()), TITLE_MAX_LENGTH),
        detail=clip((detail or "").strip(), DETAIL_MAX_LENGTH), category=category,
        severity=severity, prompt_text=clip((prompt_text or "").strip(), PROMPT_TEXT_MAX_LENGTH),
        evidence=[], created_by=user,
    )
    return _reloaded([finding])[0]


def _reloaded(findings: list) -> list:
    """The findings with the relations the serializer reads, in order."""
    if not findings:
        return []
    ids = [f.id for f in findings]
    from apps.agent.models import AgentFinding

    rows = {f.id: f for f in AgentFinding.objects.select_related(
        "source_swarm", "source_chat", "created_by", "status_changed_by").filter(id__in=ids)}
    return [rows[i] for i in ids if i in rows]


# ── Changing ──────────────────────────────────────────────────────────────


def update_finding(finding, user, **fields):
    """Change any of status, severity, title and detail. A status change
    records when and by whom."""
    changed = []
    if "title" in fields:
        finding.title = clip(" ".join(fields["title"].split()), TITLE_MAX_LENGTH)
        changed.append("title")
    if "detail" in fields:
        finding.detail = clip((fields["detail"] or "").strip(), DETAIL_MAX_LENGTH)
        changed.append("detail")
    if "severity" in fields:
        finding.severity = fields["severity"]
        changed.append("severity")
    if "status" in fields and fields["status"] != finding.status:
        finding.status = fields["status"]
        finding.status_changed_at = timezone.now()
        finding.status_changed_by = user
        changed += ["status", "status_changed_at", "status_changed_by"]
    if changed:
        finding.save(update_fields=[*changed, "updated_at"])
    return _reloaded([finding])[0]


def delete_finding(finding) -> None:
    finding.delete()


# ── Cursor ────────────────────────────────────────────────────────────────


def _encode_cursor(at: datetime, finding_id) -> str:
    raw = json.dumps([at.isoformat(), str(finding_id)], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        at_text, id_text = json.loads(raw)
        at = parse_datetime(at_text)
        finding_id = uuid.UUID(id_text)
    except (binascii.Error, ValueError, TypeError, AttributeError, UnicodeDecodeError):
        raise InvalidCursor() from None
    if at is None or timezone.is_naive(at):
        raise InvalidCursor()
    return at, finding_id
