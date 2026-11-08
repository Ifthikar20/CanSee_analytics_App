"""Save an agent reply to Reports.

The report is the reply's Markdown plus a snapshot of the research behind
it: the Google results pages and pages it inspected (from the message's
research store), the AI engine checks (its engine_check parts), the
prompt gaps it read and, when the reply ran the research agents, their
results table (its last swarm_table part), which the report Markdown
also ends with as a "Research agents" appendix. Nothing is recomputed; a
report shows exactly what the reply was written from.

The data also carries ``template`` (the report template key the reply was
written with, or None) and ``header``: the project, when the data was
captured, the data windows, each engine's status, the locations, prompts
and competitors, and what a change log compares. Every header value is
read from the reply or the project; a value nobody measured is left empty.
A report saved before templates existed has neither key.
"""
from __future__ import annotations

import re

from django.db import transaction
from django.utils import timezone

from apps.agent import report_templates
from apps.agent.constants import (
    ARTIFACT_TITLE_MAX_LENGTH,
    TERMINAL_TASK_STATUSES,
    AgentKey,
    ArtifactKind,
    MessageRole,
    MessageStatus,
    PartType,
    VersionSource,
)
from apps.agent.exceptions import AlreadySaved
from apps.agent.models import AgentArtifact, AgentMessage
from apps.agent.services import swarm_tables
from apps.agent.services.artifacts import add_version
from core.exceptions import CanseeException, ResourceNotFound

SAVABLE_STATUSES = (MessageStatus.COMPLETE, MessageStatus.TRUNCATED)
# The results-table sheets a report without a template keeps in its appendix.
DEFAULT_SHEETS = ("prompts", "gaps")
_HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(```|~~~)")


class NotSavable(CanseeException):
    def __init__(self, message: str):
        super().__init__(message, code="invalid", status_code=400)


def save_report(user, website, *, message_id, title: str = "") -> AgentArtifact:
    """Create a report from one of the user's finished replies.

    Raises AlreadySaved (409, ``details.artifact_id``) when the reply's
    report already exists.
    """
    with transaction.atomic():
        message = (
            AgentMessage.objects.select_for_update(of=("self",))
            .filter(pk=message_id, chat__user=user, chat__website=website)
            .first()
        )
        if message is None:
            raise ResourceNotFound("Message not found.")
        if message.role != MessageRole.ASSISTANT:
            raise NotSavable("Only the agent's replies can be saved as reports.")
        if message.status not in SAVABLE_STATUSES:
            raise NotSavable("This reply isn't finished, so it can't be saved yet.")
        existing = message.saved_report
        if existing is not None and existing.deleted_at is None:
            raise AlreadySaved(artifact_id=existing.id)

        text = report_markdown(message.parts)
        if not text:
            raise NotSavable("This reply has no text to save.")
        data = research_snapshot(message, website)
        template = report_templates.get(data["template"])
        content = text
        if data["swarm"] is not None:
            sheets = template.sheets if template else DEFAULT_SHEETS
            content = f"{text}\n\n" + swarm_tables.report_appendix(
                data["swarm"]["table"], _swarm_parts(message.parts)[0] or {}, sheets=sheets)
        artifact = AgentArtifact.objects.create(
            website=website,
            kind=ArtifactKind.REPORT,
            agent_key=AgentKey.GAP_ANALYST,
            # From the reply's own text: the appendix's heading is not its title.
            title=report_title(title, text, website, template=template),
            created_by=user,
            source_chat_id=message.chat_id,
            source_message=message,
        )
        add_version(
            artifact,
            content=content,
            data=data,
            summary=_summary(content, data),
            source=VersionSource.AGENT,
            created_by=user,
            run=getattr(message, "run", None),
            cost_usd=message.cost_usd,
        )
        message.saved_report = artifact
        message.save(update_fields=["saved_report"])
    return artifact


def report_markdown(parts) -> str:
    """The reply's text parts, in order."""
    texts = [
        str(part.get("text") or "").strip()
        for part in parts or []
        if isinstance(part, dict) and part.get("type") == PartType.TEXT
    ]
    return "\n\n".join(t for t in texts if t).replace("\x00", "")


def research_snapshot(message: AgentMessage, website=None) -> dict:
    """{serp, pages, engines, gaps, swarm, template, header} as captured
    while the reply was written (``website`` defaults to the chat's).

    Captures appear in the order their parts appear in the reply; any
    capture no part points at follows, oldest first. Page captures carry
    no reader text. ``swarm`` is the research agents' last results table
    (contract 6e), or None.
    """
    research = message.research if isinstance(message.research, dict) else {}
    parts = [p for p in (message.parts or []) if isinstance(p, dict)]
    serp = _ordered(research.get("serp"), parts, PartType.SERP, "serp:")
    pages = [
        {k: v for k, v in capture.items() if k != "reader"}
        for capture in _ordered(research.get("pages"), parts, PartType.PAGE_INSPECT, "page:")
    ]
    gaps = research.get("gaps")
    template = report_templates.get(research.get("template"))
    data = {
        "serp": serp,
        "pages": pages,
        "engines": [p for p in parts if p.get("type") == PartType.ENGINE_CHECK],
        "gaps": gaps if isinstance(gaps, list) else [],
        "swarm": swarm_snapshot(parts),
        "template": template.key if template else None,
    }
    data["header"] = report_header(message, website or message.chat.website, data, template)
    return data


def _swarm_parts(parts) -> tuple[dict | None, dict | None]:
    """(the swarm part, the swarm_table part) of the reply's last results
    table: the table part last in the reply, and the swarm part of the
    same swarm and version."""
    parts = [p for p in parts or [] if isinstance(p, dict)]
    table_part = next((p for p in reversed(parts) if p.get("type") == PartType.SWARM_TABLE
                       and isinstance(p.get("table"), dict)), None)
    if table_part is None:
        return None, None
    swarm_part = next((p for p in reversed(parts) if p.get("type") == PartType.SWARM
                       and p.get("swarm_id") == table_part.get("swarm_id")
                       and p.get("version") == table_part.get("version")), None)
    return swarm_part, table_part


def _task_counts(tasks) -> dict:
    statuses = [t.get("status") for t in tasks if isinstance(t, dict)]
    counts = {"total": len(statuses)}
    for status in TERMINAL_TASK_STATUSES:
        counts[status] = sum(1 for s in statuses if s == status)
    return counts


def swarm_snapshot(parts) -> dict | None:
    """``data.swarm`` of a report (contract 6e): the results table and the
    run it came from, with per-category agent counts."""
    swarm_part, table_part = _swarm_parts(parts)
    if table_part is None:
        return None
    table = table_part["table"]
    swarm_part = swarm_part or {}
    tasks = [t for t in swarm_part.get("tasks") or [] if isinstance(t, dict)]
    return {
        "swarm_id": table_part.get("swarm_id") or table.get("swarm_id"),
        "version": table_part.get("version") or table.get("version"),
        "title": table_part.get("title") or table.get("title") or "",
        "status": table_part.get("status") or table.get("status"),
        "duration_ms": swarm_part.get("duration_ms"),
        "counts": table.get("counts") or _task_counts(tasks),
        "categories": [
            {
                "key": category.get("key"),
                "label": category.get("label"),
                "counts": _task_counts(t for t in tasks
                                       if t.get("category") == category.get("key")),
            }
            for category in swarm_part.get("categories") or [] if isinstance(category, dict)
        ],
        "table": table,
    }


def report_title(given: str, content: str, website, *, template=None) -> str:
    """The given title, else the reply's first heading, else a dated
    default named after the report template (or "SEO and GEO report")."""
    title = " ".join((given or "").split())
    if not title:
        title = _first_heading(content)
    if not title:
        today = timezone.localdate()
        name = template.name if template else "SEO and GEO report"
        title = f"{name} · {website.name} · {today:%b} {today.day}, {today.year}"
    return title[:ARTIFACT_TITLE_MAX_LENGTH]


def _ordered(store, parts: list[dict], part_type: str, prefix: str) -> list[dict]:
    captures = store if isinstance(store, dict) else {}
    out, used = [], set()
    for part in parts:
        tab_id = str(part.get("tab_id") or "")
        if part.get("type") != part_type or not tab_id.startswith(prefix):
            continue
        capture_id = tab_id[len(prefix):]
        capture = captures.get(capture_id)
        if isinstance(capture, dict) and capture_id not in used:
            used.add(capture_id)
            out.append(capture)
    rest = [
        capture for capture_id, capture in captures.items()
        if capture_id not in used and isinstance(capture, dict)
    ]
    rest.sort(key=lambda c: (str(c.get("captured_at") or ""), c.get("page") or 0))
    return out + rest


def _first_heading(content: str) -> str:
    in_fence = False
    for line in content.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _HEADING.match(line)
        if match:
            text = match.group(1).replace("**", "").replace("__", "").replace("`", "")
            text = " ".join(text.split())
            if text:
                return text
    return ""


def _summary(content: str, data: dict) -> str:
    def count(n: int, noun: str, suffix: str = "") -> str:
        return f"{n} {noun}{'' if n == 1 else 's'}{suffix}"

    bits = []
    if data.get("swarm"):
        bits.append(count(int((data["swarm"].get("counts") or {}).get("total") or 0),
                          "research agent"))
    if data["serp"]:
        bits.append(count(len(data["serp"]), "Google results page"))
    if data["pages"]:
        bits.append(count(len(data["pages"]), "page", " inspected"))
    if data["engines"]:
        bits.append(count(len(data["engines"]), "AI engine check"))
    sections = sum(1 for line in content.splitlines() if line.startswith("## "))
    if sections:
        bits.append(count(sections, "section"))
    return ", ".join(bits)


# ── The report header ─────────────────────────────────────────────────────

# Read tools whose data covers a window of days, by the source they read.
_WINDOWED = {
    "get_project_overview": ("tracked_data", "Tracked prompt data",
                             report_templates.OVERVIEW_WINDOW_DAYS),
    "list_tracked_prompts": ("tracked_data", "Tracked prompt data",
                             report_templates.OVERVIEW_WINDOW_DAYS),
    "get_prompt_gap": ("tracked_data", "Tracked prompt data",
                       report_templates.OVERVIEW_WINDOW_DAYS),
    "get_ai_answers": ("engine_answers", "Saved AI engine answers",
                       report_templates.OVERVIEW_WINDOW_DAYS),
    "get_cited_sources": ("citations", "Cited sources", report_templates.OVERVIEW_WINDOW_DAYS),
    "get_brand_perception": ("perception", "Sentiment and Fact check",
                             report_templates.PERCEPTION_WINDOW_DAYS),
    "get_visibility_history": ("visibility_history", "Visibility history",
                               report_templates.HISTORY_WINDOW_DAYS),
}
# Engine statuses, the strongest first: one asked answer wins.
ENGINE_STATUSES = ("asked", "failed", "not_selected", "not_configured")
# A results-table engine cell's state: "ok" is named, "not_found" not named.
_CELL_STATUS = {"ok": "asked", "not_found": "asked", "failed": "failed", "stopped": "failed", "skipped": "failed",
                "not_selected": "not_selected", "not_configured": "not_configured"}


def report_header(message, website, data: dict, template) -> dict:
    """``data.header`` of a report (see the module docstring)."""
    parts = [p for p in (message.parts or []) if isinstance(p, dict)]
    research = message.research if isinstance(message.research, dict) else {}
    table = ((data.get("swarm") or {}).get("table")) or {}
    swarm = _swarm_of(website, table.get("swarm_id"))
    plan = swarm.plan if swarm is not None and isinstance(swarm.plan, dict) else {}
    prompts = _header_prompts(data, plan, parts)
    return {
        "template": {"id": template.key, "name": template.name} if template else None,
        "project": {"name": website.name, "url": website.url},
        "captured_at": table.get("generated_at") or (
            message.finished_at.isoformat() if message.finished_at else None),
        "data": _data_sources(parts, data),
        "engines": _engine_statuses(parts, table),
        "locations": list(dict.fromkeys(
            [str(c.get("location")).upper() for c in data.get("serp") or []
             if c.get("location")]
            + [p["location"] for p in prompts if p["location"]])),
        "prompts": prompts,
        "competitors": _header_competitors(website, plan),
        "compared": _compared(swarm, table, research),
    }


def _swarm_of(website, swarm_id):
    from django.core.exceptions import ValidationError

    from apps.agent.models import AgentSwarm

    if not swarm_id:
        return None
    try:
        return AgentSwarm.objects.filter(id=swarm_id, website=website).first()
    except (ValueError, TypeError, ValidationError):
        return None


def _data_sources(parts: list[dict], data: dict) -> list[dict]:
    """The data the reply read: windowed sources first, then captures."""
    found: dict[str, dict] = {}

    def add(key: str, label: str, window_days) -> None:
        found.setdefault(key, {"key": key, "label": label, "window_days": window_days})

    for part in parts:
        if part.get("type") == PartType.TOOL and part.get("status") == "done":
            spec = _WINDOWED.get(part.get("name"))
            if spec:
                add(*spec)
    if data.get("gaps"):
        add(*_WINDOWED["get_prompt_gap"])
    if data.get("swarm"):
        add("research_agents", "Research agents run for this reply", None)
    if data.get("serp"):
        add("google", "Google results captured for this reply", None)
    if any(p.get("status") == "done" for p in data.get("engines") or []):
        add("engine_checks", "AI engine answers captured for this reply", None)
    if data.get("pages"):
        add("pages", "Pages checked for this reply", None)
    return list(found.values())


def _engine_statuses(parts: list[dict], table: dict) -> list[dict]:
    """Each engine with what happened to it in this reply: asked, failed,
    not_selected or not_configured, else not_asked; ``configured`` is
    whether it has a key now."""
    from apps.agent.tools.research import NOT_SELECTED, engine_catalog

    seen: dict[str, set] = {}
    for part in parts:
        if part.get("type") != PartType.ENGINE_CHECK:
            continue
        status = {"done": "asked", "error": "failed"}.get(part.get("status"))
        if part.get("status") == "unavailable":
            status = "not_selected" if part.get("error") == NOT_SELECTED else "not_configured"
        if status:
            seen.setdefault(str(part.get("engine")), set()).add(status)
    prompts = next((s for s in table.get("sheets") or [] if s.get("key") == "prompts"), None)
    for row in (prompts or {}).get("rows") or []:
        for key, cell in (row.get("cells") or {}).items():
            status = _CELL_STATUS.get((cell or {}).get("state"))
            if status:
                seen.setdefault(key, set()).add(status)
    out = []
    for engine in engine_catalog():
        statuses = seen.get(engine["key"], set())
        status = next((s for s in ENGINE_STATUSES if s in statuses), None)
        out.append({
            "key": engine["key"],
            "label": engine["label"],
            "status": status or ("not_asked" if engine["configured"] else "not_configured"),
            "configured": engine["configured"],
        })
    return out


def _header_prompts(data: dict, plan: dict, parts: list[dict]) -> list[dict]:
    """The prompts the reply covered: the research plan's, the prompt
    gaps', then the engine checks', each once."""
    out: list[dict] = []
    seen: set = set()

    def add(prompt_id, text, location) -> None:
        text = " ".join(str(text or "").split())
        key = str(prompt_id or "") or text.lower()
        if not text or key in seen:
            return
        seen.add(key)
        out.append({"prompt_id": str(prompt_id) if prompt_id else None, "text": text,
                    "location": str(location or "").upper()})

    for prompt in plan.get("prompts") or []:
        if isinstance(prompt, dict):
            add(prompt.get("prompt_id"), prompt.get("text"), prompt.get("location"))
    for gap in data.get("gaps") or []:
        prompt = gap.get("prompt") if isinstance(gap, dict) else None
        if isinstance(prompt, dict):
            add(prompt.get("id"), prompt.get("text"), prompt.get("location"))
    for part in parts:
        if part.get("type") == PartType.ENGINE_CHECK:
            add(part.get("prompt_id"), part.get("prompt_text"), "")
    return out


def _header_competitors(website, plan: dict) -> list[dict]:
    """The project's tracked competitors; when the research agents were
    told which competitors to prefer, those."""
    from apps.agent.subagents.sourcing import apex_of

    tracked = [
        {"name": c["name"].strip(), "domain": (c.get("domain") or "").strip()}
        for c in website.competitors or []
        if isinstance(c, dict) and (c.get("name") or "").strip()
    ]
    preferred = [d for d in plan.get("competitors") or [] if isinstance(d, str) and d.strip()]
    if not preferred:
        return tracked
    by_apex = {apex_of(c["domain"]): c for c in tracked if c["domain"]}
    out = []
    for domain in preferred:
        match = by_apex.get(apex_of(domain))
        out.append(match or {"name": domain, "domain": domain})
    return out


def _compared(swarm, table: dict, research: dict) -> dict | None:
    """What a change log compares: two versions of the research the
    reply ran, else the engine checks get_visibility_history compared."""
    previous = table.get("previous_version")
    if swarm is not None and previous:
        from apps.agent.models import AgentSwarmVersion

        before = AgentSwarmVersion.objects.filter(swarm=swarm, version=previous).first()
        before_at = None
        if before is not None:
            before_at = (before.table or {}).get("generated_at") or (
                before.finished_at.isoformat() if before.finished_at else None)
        return {"kind": "research_versions", "version": table.get("version"),
                "previous_version": previous, "captured_at": table.get("generated_at"),
                "previous_captured_at": before_at}
    history = (research.get("history") or {}).get("visibility")
    if isinstance(history, dict):
        return {"kind": "engine_checks", "window_days": history.get("window_days"),
                "latest_check_at": history.get("latest_check_at"),
                "previous_check_at": history.get("previous_check_at")}
    return None
