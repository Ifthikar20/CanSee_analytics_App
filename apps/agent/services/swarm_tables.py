"""The research agents' consolidated table, and what a finished execution
hands on (swarm_contract.md sections 3f and 5e; swarm_backend.md 11).

finalize_version runs after every execution of a swarm (the research_swarm
tool call, or a re-run turn): it reads each prompt's Google-vs-AI gap,
builds one table from the tasks' results (this version's, and the newest
earlier result of every task that did not run now), saves it, adds the
``swarm_table`` part, and returns the tool result the orchestrator reads
plus the texts of a re-run turn.

Every value in the table comes from a task result or from
build_prompt_detail. A value nobody measured is a state ("Not measured",
"Not configured", "Failed", ...), never 0, a dash or a blank.
"""
from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC
from urllib.parse import urlsplit

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.agent.constants import (
    AI_OVERVIEW_SOURCE,
    AI_OVERVIEW_SOURCE_LABEL,
    PartType,
    SwarmCategory,
    SwarmScope,
    TaskStatus,
    VersionStatus,
)
from apps.agent.runtime.events import now_iso
from apps.agent.tools.schemas import ENGINE_KEYS
from apps.agent.tools.shaping import clip, compact, host_of, untrusted

logger = logging.getLogger("apps")

ENGINE_LABELS = {
    "chatgpt": "ChatGPT", "claude": "Claude", "gemini": "Gemini", "grok": "Grok",
    "perplexity": "Perplexity",
}
# Who cited a source (the Sources sheet's engines cell): the engines in
# catalog order, then Google's AI Overview.
SOURCE_CITER_LABELS = {**ENGINE_LABELS, AI_OVERVIEW_SOURCE: AI_OVERVIEW_SOURCE_LABEL}
SOURCE_CITER_ORDER = (*ENGINE_KEYS, AI_OVERVIEW_SOURCE)
GAP_LABELS = {
    "ai_ahead": "AI ahead",
    "google_ahead": "Google ahead",
    "strong_both": "Strong in both",
    "invisible_both": "Invisible in both",
    "even": "Even",
    "no_data": "Not checked",
}
GAP_TONES = {"ai_ahead": "warn", "google_ahead": "warn", "invisible_both": "warn",
             "strong_both": "good"}
# A gap class that is an issue to fix on its own.
ISSUE_GAP_CLASSES = ("ai_ahead", "google_ahead", "invisible_both")
STATE_TEXT = {
    "not_measured": "Not measured",
    "not_configured": "Not configured",
    "not_selected": "Not selected",
    "failed": "Failed",
    "skipped": "Skipped",
    "stopped": "Stopped",
}
# The state a cell shows when several tasks decide it and none is done:
# the most severe one.
STATE_SEVERITY = ("failed", "stopped", "skipped", "not_configured", "not_selected",
                  "not_measured")
SEVERITY_TEXT = {"high": "High", "medium": "Medium", "low": "Low"}
SEVERITY_ORDER = {"high": 1, "medium": 2, "low": 3}
NOTHING = "—"
MINUS = "−"
NOT_SELECTED_NOTE = "Not in this prompt's model selection"
SERP_NOT_CONFIGURED_NOTE = "No Google results key is set."
TRIMMED_NOTE = ("Some long texts were shortened in the results the agent read; the table "
                "shows them in full.")

PAGE_SIZE = 10
MAX_GAP_ROWS = 20
MAX_SOURCE_ROWS = 15
MAX_CHANGES = 30
MAX_EVIDENCE = 10
TITLE_MAX_CHARS = 200
DETAIL_MAX_CHARS = 2000
PROMPT_IN_TITLE_CHARS = 100
CHANGE_LABEL_CHARS = 80
SUMMARY_CHANGE_LINES = 8
SUMMARY_VALUE_CHARS = 60
TOOL_RESULT_MAX_CHARS = 16_000
WIRE_TEXT_MAX_CHARS = 8_000
TOP_RESULTS = 5
TOP_SOURCES = 5
# A task's public error message in the tool result (they are short; this
# is the bound).
PROBLEM_CHARS = 300

# Sheet columns (contract 5e): key, label, kind, align, sortable.
PROMPT_COLUMNS = [
    ("prompt", "Prompt", "text", "left", True),
    ("google", "Google", "rank", "right", True),
    ("ai_overview", "AI Overview", "status", "left", True),
    *((key, ENGINE_LABELS[key], "rank", "left", True) for key in ENGINE_KEYS),
    ("ahead", "Ahead of you", "text", "left", False),
    ("cited", "Cited sources", "text", "left", True),
    ("gap", "Gap", "class", "left", True),
    ("finding", "Finding", "text", "left", False),
]
GAP_COLUMNS = [
    ("prompt", "Prompt", "text", "left", True),
    ("issue", "Issue", "text", "left", False),
    ("fix", "Fix", "text", "left", False),
    ("page", "Page", "link", "left", True),
    ("severity", "Severity", "status", "left", True),
    ("evidence", "Evidence", "text", "left", False),
]
SOURCE_COLUMNS = [
    ("domain", "Domain", "link", "left", True),
    ("citations", "Citations", "number", "right", True),
    ("engines", "Engines", "text", "left", True),
    ("prompts", "Prompts", "number", "right", True),
    ("yours", "Yours", "status", "left", True),
    ("competitor", "Competitor", "text", "left", True),
    ("action", "Action", "text", "left", False),
]
SHEET_TITLES = {"prompts": "Prompts", "gaps": "Content gaps", "sources": "Sources"}
# The columns a Markdown copy of each sheet keeps (report appendix 6e).
MARKDOWN_COLUMNS = {
    "prompts": ("prompt", "google", "ai_overview", *ENGINE_KEYS, "gap"),
    "gaps": ("prompt", "issue", "fix", "page", "severity"),
    "sources": ("domain", "citations", "engines", "prompts", "yours", "competitor", "action"),
}
# Analysis text (the worker model's prose, not a measurement): its changes
# are listed after the measured ones and left out of a re-run's summary.
ANALYSIS_CELLS = {("prompts", "finding"), ("sources", "action")}


@dataclass
class Finalized:
    table: dict | None
    tool_result: dict
    # One line for the runner's step summary.
    summary: str
    # The visible reply of a re-run turn.
    summary_text: str
    # The assistant wire text of a re-run turn (what the model reads later).
    wire_text: str


# ── Small helpers ─────────────────────────────────────────────────────────


def format_duration(ms) -> str:
    """9s, 1m 5s, 2m, 1h 4m (the frontend's formatDuration)."""
    total = max(0, round((ms or 0) / 1000))
    if total < 60:
        return f"{total}s"
    minutes, seconds = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s" if seconds else f"{minutes}m"
    hours, rest = divmod(minutes, 60)
    return f"{hours}h {rest}m" if rest else f"{hours}h"


def and_list(items) -> str:
    """"A", "A and B", "A, B and C"."""
    items = [str(i) for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


def count_list(counts: dict) -> str:
    """"18 done, 1 failed, 2 skipped": zero counts left out except done."""
    parts = [f"{counts.get('done', 0)} done"]
    for key in ("failed", "skipped", "stopped"):
        if counts.get(key):
            parts.append(f"{counts[key]} {key}")
    return ", ".join(parts)


def signed(value: int) -> str:
    """+45, +0 or −45 (a real minus sign)."""
    return f"+{value}" if value >= 0 else f"{MINUS}{abs(value)}"


def gap_text(gap: dict) -> str | None:
    """"Google ahead (−45)" for a measured gap, else None."""
    cls = (gap or {}).get("class")
    value = (gap or {}).get("value")
    if not cls or cls == "no_data" or value is None:
        return None
    return f"{GAP_LABELS.get(cls, cls)} ({signed(int(value))})"


def host_path(url: str) -> str:
    """notion.so/product: a page's host and path, for a link cell."""
    try:
        path = (urlsplit(url).path or "").rstrip("/")
    except ValueError:
        path = ""
    return clip(f"{host_of(url)}{path}", 80) or clip(url, 80)


def _http_url(value) -> str | None:
    if isinstance(value, str) and value.lower().startswith(("http://", "https://")):
        return value
    return None


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def _detail(task: dict | None) -> dict:
    detail = (task or {}).get("detail")
    return detail if isinstance(detail, dict) else {}


def _error(task: dict | None) -> tuple[str | None, str | None]:
    error = (task or {}).get("error")
    if isinstance(error, dict):
        return error.get("code"), error.get("message")
    return None, (str(error) if error else None)


def _cell(text, *, version: int, v=None, state: str = "ok", tone=None, note=None,
          task_id=None) -> dict:
    return {"text": text, "v": v, "state": state, "tone": tone, "note": note,
            "task_id": task_id, "version": version, "previous": None}


def _state_cell(state: str, *, version: int, note=None, task_id=None) -> dict:
    return _cell(STATE_TEXT[state], version=version, state=state,
                 tone="bad" if state == "failed" else "muted", note=note, task_id=task_id)


def _columns(spec) -> list[dict]:
    return [{"key": key, "label": label, "kind": kind, "align": align, "sortable": sortable}
            for key, label, kind, align, sortable in spec]


# ── The tasks behind one table ────────────────────────────────────────────


@dataclass
class _Ran:
    task: dict
    # The version whose execution produced this result.
    version: int

    @property
    def status(self) -> str | None:
        return self.task.get("status")

    @property
    def done(self) -> bool:
        return self.status == TaskStatus.DONE

    @property
    def detail(self) -> dict:
        return _detail(self.task)


class _Inputs:
    """The plan, the tasks per prompt and every task's newest result."""

    def __init__(self, swarm, version, outcome, gaps: dict | None):
        from apps.agent.tools.research import engine_catalog

        self.number = version.version
        self.plan = swarm.plan if isinstance(swarm.plan, dict) else {}
        self.prompts = [p for p in self.plan.get("prompts") or [] if isinstance(p, dict)]
        self.specs = [t for t in swarm.tasks or [] if isinstance(t, dict) and t.get("id")]
        self.by_prompt: dict[str, list[dict]] = defaultdict(list)
        for spec in self.specs:
            self.by_prompt[str(spec.get("subcategory") or "")].append(spec)
        self.ran: dict[str, _Ran] = {}
        for task_id, entry in (getattr(outcome, "carried", None) or {}).items():
            task = entry.get("task") if isinstance(entry, dict) else None
            if isinstance(task, dict):
                self.ran[task_id] = _Ran(task, int(entry.get("version") or 0))
        for task_id, task in (getattr(outcome, "results", None) or {}).items():
            if isinstance(task, dict):
                self.ran[task_id] = _Ran(task, self.number)
        self.carried_versions = sorted({
            int(entry.get("version") or 0)
            for entry in (getattr(outcome, "carried", None) or {}).values()
            if isinstance(entry, dict) and isinstance(entry.get("task"), dict)
        })
        self.gaps = gaps or {}
        self.key_settings = {e["key"]: e["key_setting"] for e in engine_catalog()}
        self.not_configured = set(self.plan.get("engines_not_configured") or [])
        wanted = self.plan.get("engines")
        self.wanted = set(wanted) if isinstance(wanted, list | tuple) else None

    # ── lookups ──

    def specs_of(self, key: str, category: str, *, engine: str | None = None) -> list[dict]:
        out = [s for s in self.by_prompt.get(key, []) if s.get("category") == category]
        if engine is not None:
            out = [s for s in out if (s.get("inputs") or {}).get("engine") == engine]
        return out

    def spec(self, task_id: str) -> dict | None:
        return next((s for s in self.specs if s["id"] == task_id), None)

    def result(self, spec: dict | None) -> _Ran | None:
        return self.ran.get(spec["id"]) if spec else None

    def done(self, specs: list[dict]) -> list[tuple[dict, _Ran]]:
        return [(s, r) for s in specs if (r := self.ran.get(s["id"])) is not None and r.done]

    def gap(self, key: str) -> dict:
        gap = (self.gaps.get(key) or {}).get("gap")
        return gap if isinstance(gap, dict) else {}

    def status_cell(self, pairs: list[tuple[dict, _Ran]]) -> dict:
        """The most severe state among tasks none of which is done."""
        best = None
        for spec, ran in pairs:
            code, message = _error(ran.task)
            if ran.status == TaskStatus.SKIPPED and code in ("not_configured", "not_selected"):
                state = code
            elif ran.status in (TaskStatus.FAILED, TaskStatus.SKIPPED, TaskStatus.STOPPED):
                state = ran.status
            else:
                state = "not_measured"
            rank = STATE_SEVERITY.index(state)
            if best is None or rank < best[0]:
                best = (rank, state, message, spec["id"], ran.version)
        if best is None:
            return _state_cell("not_measured", version=self.number)
        _, state, message, task_id, version = best
        return _state_cell(state, version=version, note=message, task_id=task_id)


# ── Prompts sheet ─────────────────────────────────────────────────────────


def _engine_rank_key(ran: _Ran) -> tuple:
    detail = ran.detail
    if detail.get("mentioned"):
        rank = detail.get("rank")
        return (0, rank) if rank else (1, 0)
    return (2, 0)


def _brand_names(items, limit: int) -> list[str]:
    names = []
    for item in items or []:
        name = (item.get("name") if isinstance(item, dict) else item) or ""
        name = " ".join(str(name).split())
        if name and name not in names:
            names.append(name)
        if len(names) == limit:
            break
    return names


class _PromptRow:
    """The cells, finding draft and links of one prompt's row."""

    def __init__(self, inputs: _Inputs, prompt: dict):
        self.inputs = inputs
        self.prompt = prompt
        self.key = str(prompt.get("key") or "")
        self.text = " ".join(str(prompt.get("text") or "").split())
        self.number = inputs.number

    # ── cells ──

    def prompt_cell(self) -> dict:
        location = str(self.prompt.get("location") or "").upper()
        note = f"Searched from {location}" if location else "Global"
        return _cell(self.text, version=self.number, v=self.text, note=note)

    def google_pages(self) -> tuple[list[dict], list[tuple[dict, _Ran]]]:
        specs = self.inputs.specs_of(self.key, SwarmCategory.GOOGLE)
        return specs, self.inputs.done(specs)

    def google_pages_read(self) -> list[int]:
        """The page numbers of the prompt's done Google pages, in order."""
        _, done = self.google_pages()
        return sorted({int((spec.get("inputs") or {}).get("page") or ran.detail.get("page") or 1)
                       for spec, ran in done})

    def google_from_page_one(self) -> bool:
        """Whether the done Google pages run from page 1 without a hole, so
        they cover the top results: a claim of absence or of who ranks
        ahead needs every page before it (page 1 failed, page 2 done does
        not tell whether the brand is in the top 10)."""
        pages = self.google_pages_read()
        return bool(pages) and pages == list(range(1, len(pages) + 1))

    def google_absence(self) -> tuple[str, str]:
        """(cell text, finding title phrase) for a brand the done Google
        pages do not list: "Not in top 20" over pages from page 1, else the
        pages that were read ("Not on page 2")."""
        pages = self.google_pages_read()
        if self.google_from_page_one():
            top = PAGE_SIZE * len(pages)
            return f"Not in top {top}", f"Not in Google's top {top}"
        where = f"page {pages[0]}" if len(pages) == 1 else f"pages {and_list(pages)}"
        return f"Not on {where}", f"Not on Google {where}"

    def google_cell(self) -> dict:
        specs, done = self.google_pages()
        if not specs:
            return _state_cell("not_configured", version=self.number,
                               note=SERP_NOT_CONFIGURED_NOTE)
        measured = [(s, r) for s in specs if (r := self.inputs.result(s)) is not None]
        if not measured:
            return _state_cell("not_measured", version=self.number)
        if not done:
            return self.inputs.status_cell(measured)
        version = max(r.version for _, r in done)
        note = f"{len(done)} of {len(specs)} pages searched"
        found = []
        for spec, ran in done:
            found += [(p, spec["id"]) for p in ran.detail.get("brand_positions") or []
                      if isinstance(p, int) and not isinstance(p, bool)]
        if found:
            best, task_id = min(found)
            return _cell(f"#{best}", version=version, v=best, tone="good" if best <= 3 else None,
                         note=note, task_id=task_id)
        return _cell(self.google_absence()[0], version=version, v=1000,
                     state="not_found", tone="warn", note=note, task_id=done[0][0]["id"])

    def ai_overview_cell(self) -> dict:
        from apps.agent.tools.research import DEFERRED_OVERVIEW_NOTE

        specs, _ = self.google_pages()
        if not specs:
            return _state_cell("not_configured", version=self.number,
                               note=SERP_NOT_CONFIGURED_NOTE)
        spec = self.inputs.spec(f"google.{self.key}.page1") or specs[0]
        ran = self.inputs.result(spec)
        if ran is None:
            return _state_cell("not_measured", version=self.number)
        if not ran.done:
            return self.inputs.status_cell([(spec, ran)])
        overview = ran.detail.get("ai_overview")
        cell = {"version": ran.version, "task_id": spec["id"]}
        if not isinstance(overview, dict) or not overview.get("shown"):
            return _cell("Not shown", v=0, **cell)
        if overview.get("content_captured") is False:
            return _cell("Shown, not read", v=1, tone="muted", note=DEFERRED_OVERVIEW_NOTE, **cell)
        if overview.get("mentions_you"):
            text = "Names you · cites you" if overview.get("cites_you") else "Names you"
            return _cell(text, v=3, tone="good", **cell)
        if overview.get("cites_you"):
            return _cell("Cites you", v=3, tone="good", **cell)
        return _cell("Shown, not named", v=2, tone="warn", **cell)

    def engine_cell(self, engine: str) -> dict:
        inputs = self.inputs
        if engine in inputs.not_configured:
            setting = inputs.key_settings.get(engine)
            return _state_cell("not_configured", version=self.number,
                               note=f"{setting} is not set." if setting else None)
        if inputs.wanted is not None and engine not in inputs.wanted:
            return _state_cell("not_measured", version=self.number,
                               note="Not asked in this research.")
        specs = inputs.specs_of(self.key, SwarmCategory.ENGINES, engine=engine)
        if not specs:
            return _state_cell("not_selected", version=self.number, note=NOT_SELECTED_NOTE)
        measured = [(s, r) for s in specs if (r := inputs.result(s)) is not None]
        if not measured:
            return _state_cell("not_measured", version=self.number)
        done = [(s, r) for s, r in measured if r.done]
        if not done:
            return inputs.status_cell(measured)
        spec, ran = min(done, key=lambda pair: _engine_rank_key(pair[1]))
        detail = ran.detail
        cell = {"version": ran.version, "task_id": spec["id"]}
        if detail.get("mentioned"):
            rank = detail.get("rank")
            sentiment = str(detail.get("sentiment") or "").strip()
            built = _cell(f"Named #{rank}" if rank else "Named", v=rank or 100,
                          tone="good" if rank and rank <= 3 else None,
                          note=sentiment.capitalize() or None, **cell)
        else:
            names = _brand_names(detail.get("brands"), 2)
            built = _cell("Not named", v=1000, state="not_found", tone="warn",
                          note=f"Lists {', '.join(names)}" if names else None, **cell)
        others = len(specs) - 1
        if others:
            more = f"+{others} more model{'s' if others > 1 else ''}"
            built["note"] = f"{built['note']} · {more}" if built["note"] else more
        return built

    def ahead_cell(self) -> dict:
        """Brands ahead of you: per engine that answered, and the best
        competitor result above your Google position (only when the pages
        read run from page 1: with page 1 unread, nobody on page 2 is known
        to rank ahead of you)."""
        inputs = self.inputs
        brands: dict[str, dict] = {}
        measured = False
        versions = []

        def entry(name: str) -> dict:
            return brands.setdefault(name.casefold(), {
                "name": name, "engines": set(), "google": None, "order": len(brands),
            })

        for engine in ENGINE_KEYS:
            for _spec, ran in inputs.done(inputs.specs_of(self.key, SwarmCategory.ENGINES,
                                                          engine=engine)):
                measured = True
                versions.append(ran.version)
                for name in _brand_names(ran.detail.get("brands_above"), 10):
                    entry(name)["engines"].add(engine)
        _, pages = self.google_pages()
        if pages and self.google_from_page_one():
            measured = True
            versions += [r.version for _, r in pages]
            own = [p for _, r in pages for p in r.detail.get("brand_positions") or []
                   if isinstance(p, int)]
            best_own = min(own) if own else None
            rivals = [
                row for _, r in pages for row in r.detail.get("top") or []
                if isinstance(row, dict) and row.get("is_competitor")
                and isinstance(row.get("position"), int)
                and (best_own is None or row["position"] < best_own)
            ]
            if rivals:
                top = min(rivals, key=lambda row: row["position"])
                name = " ".join(str(top.get("competitor_name") or top.get("domain") or "").split())
                if name:
                    found = entry(name)
                    if found["google"] is None or top["position"] < found["google"]:
                        found["google"] = top["position"]
        version = max(versions) if versions else self.number
        if not measured:
            return _state_cell("not_measured", version=self.number)
        if not brands:
            return _cell("None", version=version, v=None)
        ranked = sorted(
            brands.values(),
            key=lambda b: (-(len(b["engines"]) + (1 if b["google"] else 0)),
                           b["google"] or 999, b["order"]),
        )[:3]
        pieces = []
        for b in ranked:
            why = []
            if b["engines"]:
                why.append(_plural(len(b["engines"]), "engine"))
            if b["google"]:
                why.append(f"Google #{b['google']}")
            pieces.append(f"{b['name']} ({', '.join(why)})")
        return _cell(", ".join(pieces), version=version, v=None)

    def cited_cell(self) -> dict:
        inputs = self.inputs
        if not inputs.plan.get("citations", True):
            return _state_cell("not_measured", version=self.number,
                               note="Citations were not requested.")
        spec = inputs.spec(f"sources.{self.key}.cited")
        if spec is None:
            return _state_cell("not_measured", version=self.number,
                               note="No AI engine was asked this prompt.")
        ran = inputs.result(spec)
        if ran is None:
            return _state_cell("not_measured", version=self.number)
        cell = {"version": ran.version, "task_id": spec["id"]}
        if ran.done:
            total = ran.detail.get("total_citations")
            total = total if isinstance(total, int) else len(ran.detail.get("sources") or [])
            if ran.detail.get("you_cited"):
                return _cell(f"{_plural(total, 'source')} · you're cited", v=total, tone="good",
                             **cell)
            return _cell(f"{_plural(total, 'source')} · not you", v=total, tone="warn", **cell)
        code, _ = _error(ran.task)
        if ran.status == TaskStatus.SKIPPED and code == "nothing_to_analyze":
            return _cell("None cited", v=0, **cell)
        return inputs.status_cell([(spec, ran)])

    def gap_cell(self) -> dict:
        gap = self.inputs.gap(self.key)
        text = gap_text(gap)
        if text is None:
            return _cell("Not checked", version=self.number, state="not_measured", tone="muted")
        note = (f"AI score {gap.get('ai_score')}, search score {gap.get('search_score')}; from "
                "the latest answers and Google check")
        return _cell(text, version=self.number, v=int(gap["value"]),
                     tone=GAP_TONES.get(gap.get("class")), note=note)

    def finding_cell(self) -> dict:
        inputs = self.inputs
        spec = inputs.spec(f"site.{self.key}.gaps")
        ran = inputs.result(spec)
        if ran is not None and ran.done and ran.detail.get("headline"):
            headline = str(ran.detail["headline"])
            return _cell(headline, version=ran.version, v=headline, task_id=spec["id"])
        for engine_spec in inputs.specs_of(self.key, SwarmCategory.ENGINES):
            engine_ran = inputs.result(engine_spec)
            analysis = engine_ran.detail.get("analysis") if engine_ran and engine_ran.done else None
            opportunity = (analysis or {}).get("opportunity") if isinstance(analysis, dict) else None
            if opportunity:
                return _cell(str(opportunity), version=engine_ran.version, v=str(opportunity),
                             task_id=engine_spec["id"])
        if not inputs.plan.get("content_gaps", True):
            return _cell(NOTHING, version=self.number)
        if spec is None or ran is None:
            return _state_cell("not_measured", version=self.number)
        if ran.done:
            return _cell("No gaps found", version=ran.version, v="No gaps found",
                         task_id=spec["id"])
        return inputs.status_cell([(spec, ran)])

    # ── the row ──

    def cells(self) -> dict:
        cells = {
            "prompt": self.prompt_cell(),
            "google": self.google_cell(),
            "ai_overview": self.ai_overview_cell(),
        }
        for engine in ENGINE_KEYS:
            cells[engine] = self.engine_cell(engine)
        cells["ahead"] = self.ahead_cell()
        cells["cited"] = self.cited_cell()
        cells["gap"] = self.gap_cell()
        cells["finding"] = self.finding_cell()
        return cells

    def evidence(self) -> tuple[list[dict], list[dict]]:
        """(evidence, links): the prompt's Google pages and pages read, then
        its engine answers (evidence only)."""
        inputs = self.inputs
        links: list[dict] = []
        seen: set[str] = set()
        _, pages = self.google_pages()
        for spec, ran in pages:
            pane = ran.task.get("pane") or {}
            tab_id = pane.get("tab_id")
            if not tab_id or tab_id in seen:
                continue
            seen.add(tab_id)
            page = (spec.get("inputs") or {}).get("page") or ran.detail.get("page") or 1
            positions = [p for p in ran.detail.get("brand_positions") or [] if isinstance(p, int)]
            label = f"Google page {page}"
            if positions:
                label += f" · you're #{min(positions)}"
            links.append({"kind": "serp", "label": label, "tab_id": tab_id, "url": None})
        page_specs = (inputs.specs_of(self.key, SwarmCategory.SITE)
                      + inputs.specs_of(self.key, SwarmCategory.COMPETITORS))
        for _spec, ran in inputs.done([s for s in page_specs if s.get("worker") == "page_inspect"]):
            item = _page_item(ran)
            if item and (item["tab_id"] or item["url"]) and item["tab_id"] not in seen:
                if item["tab_id"]:
                    seen.add(item["tab_id"])
                links.append(item)
        answers = []
        for spec, ran in inputs.done(inputs.specs_of(self.key, SwarmCategory.ENGINES)):
            detail = ran.detail
            if detail.get("mentioned"):
                rank = detail.get("rank")
                how = f"named #{rank}" if rank else "named"
            else:
                how = "not named"
            answers.append({"kind": "answer", "label": f"{spec.get('label')} answer · {how}",
                            "tab_id": None, "url": None})
        return (links + answers)[:MAX_EVIDENCE], links[:MAX_EVIDENCE]

    def task_ids(self) -> list[str]:
        return [s["id"] for s in self.inputs.by_prompt.get(self.key, [])
                if s["id"] in self.inputs.ran]

    def finding(self, cells: dict, evidence: list[dict]) -> dict | None:
        """The draft a user can add to Findings; None when nothing measured
        is an issue (named by every engine that answered, found on Google,
        and no gap class that is an issue)."""
        answered = [e for e in ENGINE_KEYS if cells[e]["state"] in ("ok", "not_found")]
        not_named = [e for e in answered if cells[e]["state"] == "not_found"]
        google_absent = cells["google"]["state"] == "not_found"
        gap_class = self.inputs.gap(self.key).get("class")
        if not not_named and not google_absent and gap_class not in ISSUE_GAP_CLASSES:
            return None
        quoted = f"“{clip(self.text, PROMPT_IN_TITLE_CHARS)}”"
        engines = and_list(ENGINE_LABELS[e] for e in not_named)
        if not_named and google_absent:
            title = f"Missing on Google and in {engines} for {quoted}"
        elif not_named:
            title = f"Not named by {engines} for {quoted}"
        elif google_absent:
            title = f"{self.google_absence()[1]} for {quoted}"
        else:
            title = f"{GAP_LABELS.get(gap_class, gap_class)} for {quoted}"
        if (answered and len(not_named) == len(answered) and google_absent) \
                or gap_class == "invisible_both":
            severity = "high"
        elif not_named or google_absent:
            severity = "medium"
        else:
            severity = "low"
        return {
            "title": clip(title, TITLE_MAX_CHARS),
            "detail": clip(self.finding_detail(cells), DETAIL_MAX_CHARS),
            "category": SwarmCategory.ENGINES.value if not_named else SwarmCategory.GOOGLE.value,
            "severity": severity,
            "prompt_text": clip(self.text, 300),
            "evidence": evidence,
        }

    def finding_detail(self, cells: dict) -> str:
        """One plain sentence per measured column, then the content gap headline."""
        sentences = []

        def state_word(cell) -> str:
            return STATE_TEXT[cell["state"]].lower()

        google = cells["google"]
        if google["state"] == "ok":
            sentences.append(f"Google: {google['text']}, {google['note']}.")
        elif google["state"] == "not_found":
            sentences.append(f"Google: {google['text'].lower()}, {google['note']}.")
        elif google["state"] in ("failed", "skipped", "stopped"):
            sentences.append(f"Google: {state_word(google)}.")
        overview = cells["ai_overview"]
        if overview["state"] == "ok":
            text = overview["text"].replace(" · ", ", ")
            sentences.append(f"AI Overview: {text[0].lower()}{text[1:]}.")
        for engine in ENGINE_KEYS:
            cell = cells[engine]
            label = ENGINE_LABELS[engine]
            if cell["state"] == "ok":
                sentences.append(f"{label}: {cell['text'][0].lower()}{cell['text'][1:]}.")
            elif cell["state"] == "not_found":
                ran = self.inputs.ran.get(cell["task_id"] or "")
                names = _brand_names(ran.detail.get("brands") if ran else None, 3)
                lists = f"; lists {and_list(names)}" if names else ""
                sentences.append(f"{label}: not named{lists}.")
            elif cell["state"] in ("failed", "skipped", "stopped"):
                sentences.append(f"{label}: {state_word(cell)}.")
        if cells["gap"]["state"] == "ok":
            sentences.append(f"Gap: {cells['gap']['text']}.")
        ran = self.inputs.result(self.inputs.spec(f"site.{self.key}.gaps"))
        headline = str(ran.detail.get("headline") or "").strip() if ran and ran.done else ""
        if headline:
            sentences.append(headline if headline.endswith((".", "!", "?")) else f"{headline}.")
        return " ".join(sentences)

    def build(self) -> dict:
        cells = self.cells()
        evidence, links = self.evidence()
        return {
            "key": self.key,
            "cells": cells,
            "task_ids": self.task_ids(),
            "finding": self.finding(cells, evidence),
            "links": links,
        }


def _page_item(ran: _Ran) -> dict | None:
    """A ``page`` evidence item for a page task that read its page."""
    detail = ran.detail
    url = _http_url(detail.get("final_url")) or _http_url(detail.get("url"))
    pane = ran.task.get("pane") or {}
    tab_id = pane.get("tab_id") if pane.get("kind") == "page" else None
    if not url and not tab_id:
        return None
    if detail.get("role") == "own":
        label = "Your page"
    else:
        label = detail.get("domain") or host_of(url or "") or "Competitor page"
    return {"kind": "page", "label": str(label), "tab_id": tab_id, "url": url}


# ── Gaps and sources sheets ───────────────────────────────────────────────


def _gap_rows(inputs: _Inputs) -> list[dict]:
    found = []
    for index, prompt in enumerate(inputs.prompts):
        key = str(prompt.get("key") or "")
        text = " ".join(str(prompt.get("text") or "").split())
        spec = inputs.spec(f"site.{key}.gaps")
        ran = inputs.result(spec)
        if ran is None or not ran.done:
            continue
        pages = {}
        page_specs = (inputs.specs_of(key, SwarmCategory.SITE)
                      + inputs.specs_of(key, SwarmCategory.COMPETITORS))
        for page_spec, page_ran in inputs.done(
                [s for s in page_specs if s.get("worker") == "page_inspect"]):
            item = _page_item(page_ran)
            if item is None:
                continue
            for url in (page_ran.detail.get("url"), page_ran.detail.get("final_url")):
                if url:
                    pages[url] = (item, page_spec["id"])
        compared = []
        for page in ran.detail.get("compared") or []:
            url = page.get("url") if isinstance(page, dict) else None
            if url in pages and pages[url] not in compared:
                compared.append(pages[url])
            elif _http_url(url):
                label = "Your page" if page.get("role") == "own" else host_of(url) or url
                compared.append(({"kind": "page", "label": label, "tab_id": None, "url": url},
                                 None))
        page_items = [item for item, _ in compared]
        page_task_ids = [task_id for _, task_id in compared if task_id]
        for n, gap in enumerate(ran.detail.get("gaps") or [], start=1):
            if not isinstance(gap, dict):
                continue
            severity = gap.get("severity") if gap.get("severity") in SEVERITY_ORDER else "medium"
            issue = str(gap.get("issue") or "")
            fix = str(gap.get("fix") or "")
            proof = str(gap.get("evidence") or "")
            target = _http_url(gap.get("target"))
            cell = {"version": ran.version, "task_id": spec["id"]}
            cells = {
                "prompt": _cell(text, v=text, **cell),
                "issue": _cell(issue, v=issue, **cell),
                "fix": _cell(fix, v=fix, **cell),
                "page": (_cell(host_path(target), v=target, **cell) if target
                         else _cell(NOTHING, **cell)),
                "severity": _cell(SEVERITY_TEXT[severity], v=SEVERITY_ORDER[severity], **cell),
                "evidence": _cell(proof, v=proof, **cell),
            }
            evidence = list(page_items)
            if target:
                evidence.append({"kind": "link", "label": host_path(target), "tab_id": None,
                                 "url": target})
            row = {
                "key": f"{key}.g{n}",
                "cells": cells,
                "task_ids": [spec["id"], *page_task_ids],
                "finding": {
                    "title": clip(issue, TITLE_MAX_CHARS),
                    "detail": clip(f"Evidence: {proof} Fix: {fix}", DETAIL_MAX_CHARS),
                    "category": SwarmCategory.SITE.value,
                    "severity": severity,
                    "prompt_text": clip(text, 300),
                    "evidence": evidence[:MAX_EVIDENCE],
                },
                "links": evidence[:MAX_EVIDENCE],
            }
            found.append(((SEVERITY_ORDER[severity], index, n), row))
    found.sort(key=lambda pair: pair[0])
    return [row for _, row in found[:MAX_GAP_ROWS]]


def _source_rows(inputs: _Inputs) -> list[dict]:
    merged: dict[str, dict] = {}
    for prompt in inputs.prompts:
        key = str(prompt.get("key") or "")
        text = " ".join(str(prompt.get("text") or "").split())
        spec = inputs.spec(f"sources.{key}.cited")
        ran = inputs.result(spec)
        if ran is None or not ran.done:
            continue
        analysis = ran.detail.get("analysis")
        opportunities = analysis.get("opportunities") if isinstance(analysis, dict) else None
        actions = {}
        for item in opportunities or []:
            if isinstance(item, dict) and item.get("domain") and item.get("action"):
                actions.setdefault(item["domain"], (str(item["action"]), str(item.get("why") or "")))
        for source in ran.detail.get("sources") or []:
            if not isinstance(source, dict) or not source.get("domain"):
                continue
            domain = str(source["domain"])
            entry = merged.setdefault(domain, {
                "citations": 0, "engines": set(), "prompts": [], "yours": False,
                "competitor": None, "action": None, "prompt_text": "", "example_url": None,
                "task_ids": [], "versions": [],
            })
            citations = source.get("citations")
            entry["citations"] += citations if isinstance(citations, int) else 0
            entry["engines"].update(e for e in source.get("engines") or []
                                    if e in SOURCE_CITER_LABELS)
            if key not in entry["prompts"]:
                entry["prompts"].append(key)
            entry["yours"] = entry["yours"] or bool(source.get("is_yours"))
            entry["competitor"] = entry["competitor"] or source.get("competitor_name")
            entry["example_url"] = entry["example_url"] or _http_url(source.get("example_url"))
            if entry["action"] is None and domain in actions:
                entry["action"] = actions[domain]
                entry["prompt_text"] = text
            if spec["id"] not in entry["task_ids"]:
                entry["task_ids"].append(spec["id"])
            entry["versions"].append(ran.version)
    rows = []
    for domain, entry in sorted(merged.items(), key=lambda item: (-item[1]["citations"], item[0])):
        cell = {"version": max(entry["versions"]), "task_id": entry["task_ids"][0]}
        engines = ", ".join(SOURCE_CITER_LABELS[e] for e in SOURCE_CITER_ORDER
                            if e in entry["engines"])
        action, why = entry["action"] or (None, "")
        link = ({"kind": "link", "label": domain, "tab_id": None, "url": entry["example_url"]}
                if entry["example_url"] else None)
        competitor = str(entry["competitor"]) if entry["competitor"] else None
        rows.append({
            "key": domain,
            "cells": {
                "domain": _cell(domain, v=entry["example_url"], **cell),
                "citations": _cell(str(entry["citations"]), v=entry["citations"], **cell),
                "engines": _cell(engines or NOTHING, v=engines or None, **cell),
                "prompts": _cell(str(len(entry["prompts"])), v=len(entry["prompts"]), **cell),
                "yours": _cell("Yes" if entry["yours"] else "No", v=1 if entry["yours"] else 0,
                               tone="good" if entry["yours"] else None, **cell),
                "competitor": _cell(competitor or NOTHING, v=competitor, **cell),
                "action": _cell(action or NOTHING, v=action, **cell),
            },
            "task_ids": entry["task_ids"],
            "finding": {
                "title": clip(f"Get cited on {domain}", TITLE_MAX_CHARS),
                "detail": clip(f"{action} Why: {why}" if why else action, DETAIL_MAX_CHARS),
                "category": SwarmCategory.SOURCES.value,
                "severity": "medium",
                "prompt_text": clip(entry["prompt_text"], 300),
                "evidence": [link] if link else [],
            } if action else None,
            "links": [link] if link else [],
        })
        if len(rows) == MAX_SOURCE_ROWS:
            break
    return rows


# ── Versions, changes and notes ───────────────────────────────────────────


def _row_label(sheet_key: str, row: dict) -> str:
    cells = row.get("cells") or {}
    if sheet_key == "prompts":
        return (cells.get("prompt") or {}).get("text") or row.get("key")
    if sheet_key == "gaps":
        return (cells.get("issue") or {}).get("text") or row.get("key")
    return row.get("key") or ""


def apply_previous(table: dict, previous: dict | None) -> list[dict]:
    """Mark every cell whose text differs from the previous table's same
    cell (``previous``) and list those changes, the most significant first:
    on the prompts sheet the measured columns (rank, status and gap class)
    and any change of state, then its text cells, then the other sheets; at
    most MAX_CHANGES."""
    if not isinstance(previous, dict):
        return []
    before_version = previous.get("version")
    before = {}
    for sheet in previous.get("sheets") or []:
        for row in sheet.get("rows") or []:
            for column, cell in (row.get("cells") or {}).items():
                before[(sheet.get("key"), row.get("key"), column)] = cell
    found = []
    order = 0
    for sheet in table["sheets"]:
        columns = {c["key"]: c for c in sheet["columns"]}
        for row in sheet["rows"]:
            for column, cell in row["cells"].items():
                old = before.get((sheet["key"], row["key"], column))
                if not isinstance(old, dict) or old.get("text") == cell.get("text"):
                    continue
                cell["previous"] = {"text": old.get("text"), "version": before_version}
                if sheet["key"] == "prompts":
                    measured = (columns[column]["kind"] != "text"
                                or old.get("state") != cell["state"])
                    rank = 0 if measured else 1
                else:
                    rank = 2
                order += 1
                label = (f"{clip(_row_label(sheet['key'], row), CHANGE_LABEL_CHARS)} · "
                         f"{columns[column]['label']}")
                found.append((rank, order, {
                    "sheet": sheet["key"], "row": row["key"], "column": column,
                    "label": label, "from": old.get("text"), "to": cell.get("text"),
                }))
    found.sort(key=lambda item: (item[0], item[1]))
    return [change for _, _, change in found[:MAX_CHANGES]]


def _scope_note(swarm, version, inputs: _Inputs) -> str | None:
    from apps.agent.runtime.swarm import plan

    if version.scope == SwarmScope.ALL:
        return None
    label = plan.scope_label(swarm.categories or [], swarm.tasks or [], inputs.prompts,
                             version.scope, version.scope_key)
    agents = "agent" if version.scope == SwarmScope.TASK else "agents"
    head = f"Only the {label} {agents} ran in this version;"
    versions = inputs.carried_versions
    if not versions:
        return f"{head} the other agents have no results yet."
    where = f"version {versions[0]}" if len(versions) == 1 else "earlier versions"
    return f"{head} other cells are from {where}."


def _counts(version, outcome) -> dict:
    results = getattr(outcome, "results", None) or {}
    statuses = [(results.get(task_id) or {}).get("status") for task_id in version.task_ids or []]
    counts = {"total": len(statuses)}
    for status in ("done", "failed", "skipped", "stopped"):
        counts[status] = sum(1 for s in statuses if s == status)
    return counts


# ── The table ─────────────────────────────────────────────────────────────


def build_table(swarm, version, outcome, *, gaps: dict, previous: dict | None) -> dict:
    """The SwarmTable of one execution (contract 5e)."""
    inputs = _Inputs(swarm, version, outcome, gaps)
    sheets = [{
        "key": "prompts",
        "title": SHEET_TITLES["prompts"],
        "columns": _columns(PROMPT_COLUMNS),
        "rows": [_PromptRow(inputs, prompt).build() for prompt in inputs.prompts],
    }]
    for key, columns, rows in (("gaps", GAP_COLUMNS, _gap_rows(inputs)),
                               ("sources", SOURCE_COLUMNS, _source_rows(inputs))):
        if rows:
            sheets.append({"key": key, "title": SHEET_TITLES[key], "columns": _columns(columns),
                           "rows": rows})
    notes = []
    scope_note = _scope_note(swarm, version, inputs)
    if scope_note:
        notes.append(scope_note)
    for note in getattr(outcome, "notes", None) or []:
        text = note.get("text") if isinstance(note, dict) else None
        if text and text not in notes:
            notes.append(str(text))
    table = {
        "swarm_id": str(swarm.id),
        "version": version.version,
        "title": swarm.title,
        "status": outcome.status,
        "generated_at": now_iso(),
        "previous_version": previous.get("version") if isinstance(previous, dict) else None,
        "counts": _counts(version, outcome),
        "notes": notes,
        "changes": [],
        "sheets": sheets,
    }
    table["changes"] = apply_previous(table, previous)
    return table


def _sheet(table: dict, key: str) -> dict | None:
    return next((s for s in (table or {}).get("sheets") or [] if s.get("key") == key), None)


def _md(text) -> str:
    return " ".join(str(text if text is not None else "").replace("\x00", "").split()) \
        .replace("|", "\\|")


def table_markdown(table: dict, *, sheets=("prompts", "gaps")) -> str:
    """The table's sheets as Markdown tables (cell text, ``|`` escaped)."""
    blocks = []
    for key in sheets:
        sheet = _sheet(table, key)
        if not sheet or not sheet.get("rows"):
            continue
        labels = {c["key"]: c["label"] for c in sheet.get("columns") or []}
        columns = [c for c in MARKDOWN_COLUMNS.get(key, ()) if c in labels]
        lines = [
            "| " + " | ".join(_md(labels[c]) for c in columns) + " |",
            "|" + "---|" * len(columns),
        ]
        for row in sheet["rows"]:
            cells = row.get("cells") or {}
            lines.append("| " + " | ".join(_md((cells.get(c) or {}).get("text"))
                                           for c in columns) + " |")
        block = "\n".join(lines)
        if key != "prompts":
            block = f"### {sheet.get('title') or SHEET_TITLES.get(key, key)}\n\n{block}"
        blocks.append(block)
    return "\n\n".join(blocks)


def _captured(table: dict) -> str:
    at = parse_datetime(str(table.get("generated_at") or ""))
    if at is None:
        return ""
    if timezone.is_naive(at):
        at = at.replace(tzinfo=UTC)
    at = at.astimezone(UTC)
    return f"{at.day} {at:%b %Y %H:%M} UTC"


def report_appendix(table: dict, swarm_part: dict, *, sheets=("prompts", "gaps")) -> str:
    """The "Research agents" section a saved report ends with (contract
    6e), with the given sheets (the report template's; the Sources and
    citations template keeps the sources sheet too)."""
    counts = table.get("counts") or {}
    duration = format_duration((swarm_part or {}).get("duration_ms") or 0)
    captured = _captured(table)
    line = (f"Ran {_plural(counts.get('total', 0), 'agent')} in {duration}: {count_list(counts)}. "
            f"Version {table.get('version')} of “{table.get('title')}”"
            + (f", captured {captured}." if captured else "."))
    body = table_markdown(table, sheets=sheets)
    return f"## Research agents\n\n{line}\n\n{body}".rstrip()


def _measured_change(change: dict) -> bool:
    return change.get("sheet") == "prompts" and \
        (change.get("sheet"), change.get("column")) not in ANALYSIS_CELLS


def rerun_summary(swarm, version, outcome, table) -> str:
    """The visible reply of a re-run turn (backend design 11g)."""
    if table is None:
        return f"The research agents could not run: {_failure(version, outcome)['message']}"
    counts = table.get("counts") or {}
    total = counts.get("total", 0)
    if outcome.status == VersionStatus.CANCELLED:
        settled = sum(counts.get(k, 0) for k in ("done", "failed", "skipped"))
        return (f"Stopped after {settled} of {_plural(total, 'agent')}. The results table "
                f"(version {table['version']}) shows what they found.")
    text = (f"Re-ran {_plural(total, 'agent')} in {format_duration(outcome.duration_ms)}: "
            f"{count_list(counts)}. The results table is now version {table['version']}.")
    previous = table.get("previous_version")
    if previous is None:
        return text
    changes = [c for c in table.get("changes") or [] if _measured_change(c)]
    if not changes:
        return f"{text}\n\nNo measured value changed since version {previous}."
    lines = [
        f"- {c['label']}: {clip(c['from'], SUMMARY_VALUE_CHARS)} → "
        f"{clip(c['to'], SUMMARY_VALUE_CHARS)}"
        for c in changes[:SUMMARY_CHANGE_LINES]
    ]
    return f"{text}\n\nChanges since version {previous}:\n" + "\n".join(lines)


# ── The tool result ───────────────────────────────────────────────────────


def _analysis_text(analysis) -> str:
    if not isinstance(analysis, dict):
        return ""
    parts = []
    if analysis.get("summary"):
        parts.append(f"Summary: {analysis['summary']}")
    reasons = [str(r) for r in analysis.get("reasons") or [] if r]
    if reasons:
        parts.append(f"Reasons: {'; '.join(reasons)}")
    if analysis.get("opportunity"):
        parts.append(f"Opportunity: {analysis['opportunity']}")
    return " ".join(parts)


def _state_result(cell: dict, **extra) -> dict:
    out = {"state": cell["state"], **extra}
    if cell["state"] in ("failed", "skipped", "stopped") and cell.get("note"):
        out["error"] = clip(cell["note"], PROBLEM_CHARS)
    return out


class _PromptResult:
    """One prompt of the tool result, from its table row and task details."""

    def __init__(self, inputs: _Inputs, prompt: dict, row: dict, rank_checks: dict):
        self.inputs = inputs
        self.prompt = prompt
        self.row = row
        self.cells = row["cells"]
        self.key = str(prompt.get("key") or "")
        self.rank_checks = rank_checks

    def google(self) -> dict:
        from apps.agent.tools.data import ai_overview_summary

        cell = self.cells["google"]
        if cell["state"] not in ("ok", "not_found"):
            return _state_result(cell)
        pages = self.inputs.done(self.inputs.specs_of(self.key, SwarmCategory.GOOGLE))
        first = pages[0][1].detail
        overview = first.get("ai_overview")
        found = cell["state"] == "ok"
        # "ok": Google was searched; ``found`` and the position are the result.
        return {
            "state": "ok",
            "found": found,
            "best_position": cell["v"] if found else None,
            "pages_searched": len(pages),
            "rank_recorded": bool(self.rank_checks.get(self.key)),
            "ai_overview": ai_overview_summary(overview) if isinstance(overview, dict) else None,
            "top": [
                {"position": r.get("position"), "domain": r.get("domain") or "",
                 "title": clip(r.get("title"), 120), "is_yours": bool(r.get("is_yours")),
                 "is_competitor": bool(r.get("is_competitor"))}
                for r in (first.get("top") or [])[:TOP_RESULTS] if isinstance(r, dict)
            ],
        }

    def engines(self) -> dict:
        out = {}
        for engine in ENGINE_KEYS:
            cell = self.cells[engine]
            if cell["state"] not in ("ok", "not_found"):
                out[engine] = _state_result(cell)
                continue
            ran = self.inputs.ran[cell["task_id"]]
            detail = ran.detail
            mentioned = bool(detail.get("mentioned"))
            # "ok": the engine answered; ``mentioned`` and ``rank`` are the result.
            entry = {
                "state": "ok",
                "mentioned": mentioned,
                "rank": detail.get("rank") if mentioned else None,
                "sentiment": detail.get("sentiment") if mentioned else None,
                "brands_above": _brand_names(detail.get("brands_above"), 6),
            }
            spec = self.inputs.spec(cell["task_id"]) or {}
            if (spec.get("inputs") or {}).get("variant"):
                entry["model"] = detail.get("model_id") or spec.get("label")
            text = _analysis_text(detail.get("analysis"))
            if text:
                entry["analysis"] = untrusted(f"{ENGINE_LABELS[engine]} answer analysis", text,
                                              1000)
            elif detail.get("analysis_error"):
                entry["analysis_error"] = str(detail["analysis_error"])
            out[engine] = entry
        return out

    def pages(self) -> tuple[list[dict], dict | None]:
        competitors = []
        for _spec, ran in self.inputs.done(self.inputs.specs_of(self.key,
                                                                SwarmCategory.COMPETITORS)):
            detail = ran.detail
            seo = detail.get("seo") if isinstance(detail.get("seo"), dict) else {}
            url = detail.get("final_url") or detail.get("url")
            competitors.append({
                "url": url,
                "position": detail.get("position"),
                "title": untrusted(detail.get("domain") or host_of(url or ""),
                                   detail.get("title") or seo.get("title") or "", 200),
                "word_count": seo.get("word_count"),
                "structured_data": list(seo.get("structured_data") or [])[:8],
            })
        spec = self.inputs.spec(f"site.{self.key}.page")
        if spec is None:
            return competitors, None
        ran = self.inputs.result(spec)
        if ran is None:
            return competitors, {"state": "not_measured"}
        if not ran.done:
            return competitors, _state_result(self.inputs.status_cell([(spec, ran)]))
        detail = ran.detail
        seo = detail.get("seo") if isinstance(detail.get("seo"), dict) else {}
        return competitors, {
            "url": detail.get("final_url") or detail.get("url"),
            "position": detail.get("position"),
            "word_count": seo.get("word_count"),
            "structured_data": list(seo.get("structured_data") or [])[:8],
        }

    def content_gaps(self) -> dict | None:
        spec = self.inputs.spec(f"site.{self.key}.gaps")
        if spec is None:
            return None
        ran = self.inputs.result(spec)
        if ran is None:
            return {"state": "not_measured"}
        if not ran.done:
            return _state_result(self.inputs.status_cell([(spec, ran)]))
        lines = []
        if ran.detail.get("headline"):
            lines.append(str(ran.detail["headline"]))
        for n, gap in enumerate(ran.detail.get("gaps") or [], start=1):
            if isinstance(gap, dict):
                lines.append(f"{n}. {gap.get('issue')} Evidence: {gap.get('evidence')} "
                             f"Fix: {gap.get('fix')} ({gap.get('severity')})")
        if not ran.detail.get("gaps"):
            lines.append("No gaps found.")
        return untrusted("content gap analysis", "\n".join(lines), 4000)

    def sources(self) -> dict | None:
        spec = self.inputs.spec(f"sources.{self.key}.cited")
        if spec is None:
            return None
        ran = self.inputs.result(spec)
        if ran is None:
            return {"state": "not_measured"}
        cell = self.cells["cited"]
        if not ran.done:
            if cell["state"] == "ok":
                # Skipped because no engine cited anything: a measured zero.
                return {"total": 0, "you_cited": False, "top": []}
            return _state_result(cell)
        detail = ran.detail
        out = {
            "total": detail.get("total_citations"),
            "you_cited": bool(detail.get("you_cited")),
            "top": [
                {"domain": s.get("domain"), "citations": s.get("citations"),
                 "engines": list(s.get("engines") or [])}
                for s in (detail.get("sources") or [])[:TOP_SOURCES] if isinstance(s, dict)
            ],
        }
        analysis = detail.get("analysis")
        if isinstance(analysis, dict):
            lines = [str(analysis.get("headline") or "")]
            lines += [f"{o.get('domain')}: {o.get('action')} ({o.get('why')})"
                      for o in analysis.get("opportunities") or [] if isinstance(o, dict)]
            text = "\n".join(line for line in lines if line)
            if text:
                out["analysis"] = untrusted("cited sources analysis", text, 1500)
        elif detail.get("analysis_error"):
            out["analysis_error"] = str(detail["analysis_error"])
        return out

    def gap(self) -> dict:
        gap = self.inputs.gap(self.key)
        cls = gap.get("class") or "no_data"
        return {
            "class": cls,
            "label": GAP_LABELS.get(cls, cls),
            "value": gap.get("value"),
            "ai_score": gap.get("ai_score"),
            "search_score": gap.get("search_score"),
        }

    def problems(self) -> list[str]:
        out = []
        for spec in self.inputs.by_prompt.get(self.key, []):
            ran = self.inputs.ran.get(spec["id"])
            if ran is None or ran.status not in (TaskStatus.FAILED, TaskStatus.SKIPPED,
                                                 TaskStatus.STOPPED):
                continue
            _, message = _error(ran.task)
            out.append(clip(f"{spec.get('label')}: {message or STATE_TEXT[ran.status]}",
                            PROBLEM_CHARS))
        return out

    def build(self) -> dict:
        competitors, own = self.pages()
        out = {
            "prompt_id": self.prompt.get("prompt_id"),
            "text": self.cells["prompt"]["text"],
            "location": self.prompt.get("location") or "global",
            "google": self.google(),
            "engines": self.engines(),
            "competitor_pages": competitors,
        }
        if own is not None:
            out["your_page"] = own
        gaps = self.content_gaps()
        if gaps is not None:
            out["content_gaps"] = gaps
        sources = self.sources()
        if sources is not None:
            out["sources"] = sources
        out["gap"] = self.gap()
        out["problems"] = self.problems()
        return out


def _walk_untrusted(value, limit: int) -> None:
    if isinstance(value, dict):
        if value.get("untrusted") is True and isinstance(value.get("text"), str):
            value["text"] = clip(value["text"], limit)
            return
        for item in value.values():
            _walk_untrusted(item, limit)
    elif isinstance(value, list):
        for item in value:
            _walk_untrusted(item, limit)


def _fit(result: dict) -> dict:
    """At most TOOL_RESULT_MAX_CHARS of compact JSON: lists shortened, then
    texts clipped, never a field dropped silently (``clipped`` says so)."""
    prompts = result["prompts"]

    def tops(n):
        for p in prompts:
            if isinstance(p["google"].get("top"), list):
                p["google"]["top"] = p["google"]["top"][:n]
            if isinstance((p.get("sources") or {}).get("top"), list):
                p["sources"]["top"] = p["sources"]["top"][:n]

    def lists():
        for p in prompts:
            p["competitor_pages"] = p["competitor_pages"][:1]

    def shorter():
        for p in prompts:
            p["problems"] = p["problems"][:3]
            for engine in p["engines"].values():
                if "brands_above" in engine:
                    engine["brands_above"] = engine["brands_above"][:3]

    def bare():
        tops(0)
        for p in prompts:
            p["competitor_pages"] = []
            for engine in p["engines"].values():
                engine.pop("analysis", None)
            if isinstance(p.get("sources"), dict):
                p["sources"].pop("analysis", None)

    steps = (lambda: tops(3), lists, lambda: _walk_untrusted(result, 300), shorter,
             lambda: _walk_untrusted(result, 120), bare)
    if len(compact(result)) <= TOOL_RESULT_MAX_CHARS:
        return result
    result["clipped"] = True
    for step in steps:
        step()
        if len(compact(result)) <= TOOL_RESULT_MAX_CHARS:
            break
    return result


def build_tool_result(swarm, version, outcome, table, gaps) -> dict:
    """What the orchestrator reads (contract 3f): per prompt every measured
    value, third-party and analysis text marked untrusted, failures named."""
    inputs = _Inputs(swarm, version, outcome, gaps)
    plan = inputs.plan
    rows = {row["key"]: row for row in (_sheet(table, "prompts") or {}).get("rows") or []}
    rank_checks = getattr(outcome, "rank_checks", None) or {}
    prompts = [
        _PromptResult(inputs, prompt, rows[prompt.get("key")], rank_checks).build()
        for prompt in inputs.prompts if prompt.get("key") in rows
    ]
    notes = []
    for category in swarm.categories or []:
        for note in (category or {}).get("notes") or []:
            if isinstance(note, dict) and note.get("text"):
                notes.append(str(note["text"]))
    notes += [n for n in (table or {}).get("notes") or [] if n != TRIMMED_NOTE]
    result = {
        "swarm_id": str(swarm.id),
        "version": version.version,
        "title": swarm.title,
        "status": (table or {}).get("status") or outcome.status,
        "duration_s": round((outcome.duration_ms or 0) / 1000),
        "agents": (table or {}).get("counts") or _counts(version, outcome),
        "google": {"configured": bool(plan.get("serp_configured")),
                   "pages_per_prompt": plan.get("google_pages")},
        "engines_asked": [ENGINE_LABELS.get(k, k) for k in plan.get("engines_asked") or []],
        "engines_not_configured": [ENGINE_LABELS.get(k, k)
                                   for k in plan.get("engines_not_configured") or []],
        "prompts": prompts,
        "notes": list(dict.fromkeys(notes)),
        "table_shown": table is not None,
    }
    return _fit(result)


# ── Finalizing one execution ──────────────────────────────────────────────


def _failure(version, outcome) -> dict:
    """The error of an execution that could not run: the outcome's, else
    the one saved on the version row."""
    from apps.agent.models import AgentSwarmVersion

    error = getattr(outcome, "error", None)
    if isinstance(error, dict) and error.get("message"):
        return {"code": str(error.get("code") or "swarm_failed"), "message": str(error["message"])}
    row = AgentSwarmVersion.objects.filter(id=version.id).values(
        "error_code", "error_message").first() or {}
    return {
        "code": row.get("error_code") or "swarm_failed",
        "message": row.get("error_message") or "The research agents could not run.",
    }


def _record_gaps(ctx, swarm) -> dict:
    """Each prompt's Google-vs-AI gap as get_prompt_gap reads it, recorded
    on the reply (reports keep using research.gaps). A prompt no longer
    tracked has no gap."""
    from apps.agent.tools.data import trim_prompt_detail
    from apps.prompt_library.models import BrandPrompt
    from apps.search_gap.services.overview import build_prompt_detail

    prompts = [p for p in (swarm.plan or {}).get("prompts") or [] if isinstance(p, dict)]
    ids = []
    for prompt in prompts:
        try:
            ids.append(uuid.UUID(str(prompt.get("brand_prompt_id"))))
        except ValueError:
            continue
    found = {
        str(bp.id): bp
        for bp in BrandPrompt.objects.select_related("prompt", "website").filter(
            website=ctx.website, id__in=ids)
    }
    gaps = {}
    for prompt in prompts:
        bp = found.get(str(prompt.get("brand_prompt_id")))
        if bp is None:
            continue
        try:
            trimmed = trim_prompt_detail(build_prompt_detail(ctx.website, bp))
        except Exception:
            logger.warning("agent swarm: prompt gap failed for %s", bp.id, exc_info=True)
            continue
        ctx.emitter.record_gap(trimmed)
        gaps[str(prompt.get("key"))] = trimmed
    return gaps


def _previous_table(swarm, version) -> dict | None:
    from apps.agent.models import AgentSwarmVersion

    table = (AgentSwarmVersion.objects
             .filter(swarm=swarm, version__lt=version.version, table__isnull=False)
             .order_by("-version").values_list("table", flat=True).first())
    return table if isinstance(table, dict) else None


def _summary(counts: dict) -> str:
    text = f"{counts.get('done', 0)} of {counts.get('total', 0)} agents done"
    rest = [f"{counts[k]} {k}" for k in ("failed", "skipped", "stopped") if counts.get(k)]
    return ", ".join([text, *rest])


def finalize_version(ctx, swarm, version, outcome) -> Finalized:
    """Turn one finished execution into its table, its swarm_table part and
    what the caller hands on (backend design 11b)."""
    from apps.agent.models import AgentSwarm, AgentSwarmVersion

    if outcome.status == VersionStatus.ERROR:
        error = _failure(version, outcome)
        AgentSwarmVersion.objects.filter(id=version.id).update(
            status=VersionStatus.ERROR, error_code=error["code"][:64],
            error_message=error["message"],
        )
        text = rerun_summary(swarm, version, outcome, None)
        return Finalized(
            table=None,
            tool_result={"swarm_id": str(swarm.id), "version": version.version,
                         "title": swarm.title, "status": VersionStatus.ERROR.value,
                         "error": error},
            summary=error["message"], summary_text=text, wire_text=text,
        )

    gaps = _record_gaps(ctx, swarm)
    previous = _previous_table(swarm, version)
    table = build_table(swarm, version, outcome, gaps=gaps, previous=previous)
    tool_result = build_tool_result(swarm, version, outcome, table, gaps)
    if tool_result.get("clipped"):
        table["notes"].append(TRIMMED_NOTE)
    AgentSwarmVersion.objects.filter(id=version.id).update(table=table)
    AgentSwarm.objects.filter(id=swarm.id, latest_version__lt=version.version).update(
        latest_version=version.version)
    swarm.latest_version = max(swarm.latest_version or 0, version.version)
    version.table = table

    em = ctx.emitter
    em.add_part(
        PartType.SWARM_TABLE, request=outcome.request, swarm_id=str(swarm.id),
        version=version.version, title=swarm.title, status=table["status"],
        generated_at=table["generated_at"], table=table,
    )
    em.update_part(outcome.part_id, table_version=version.version)
    em.checkpoint(force=True)

    summary_text = rerun_summary(swarm, version, outcome, table)
    markdown = table_markdown(table, sheets=("prompts",))
    wire_text = clip(f"{summary_text}\n\n{markdown}" if markdown else summary_text,
                     WIRE_TEXT_MAX_CHARS)
    return Finalized(table=table, tool_result=tool_result, summary=_summary(table["counts"]),
                     summary_text=summary_text, wire_text=wire_text)
