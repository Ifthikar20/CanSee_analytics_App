"""The answers of a window of days, as the visibility dashboard reads them.

Shared by the visibility dashboard (pages contract section 5) and the
insights pages (wave 2): the engines' answers to the website's tracked
prompts, matched to the prompts through ``prompt_stats.results_by_prompt``
(a result matched to two prompts stays with the first), the period and
interval filters, the window's buckets and the per-slice tallies.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta

from apps.citations.services.url_analytics import model_key
from apps.prompt_library.services.prompt_stats import results_by_prompt
from core.exceptions import CanseeException

# Backend provider keys in display order, and their public keys.
ENGINE_ORDER = ("gpt4", "claude", "gemini", "grok", "perplexity")
ENGINE_KEYS = tuple(model_key(p) for p in ENGINE_ORDER)
PERIOD_DAYS = {"7d": 7, "30d": 30, "90d": 90}
INTERVALS = ("day", "week")
RESULT_FIELDS = (
    "id", "public_id", "provider", "prompt", "is_mentioned", "mention_rank", "sentiment",
    "competitors_mentioned", "query_succeeded", "created_at", "source_prompt_id",
)


def _invalid(field: str, message: str) -> CanseeException:
    return CanseeException(
        message, code="invalid", status_code=400, details={"field": field, "message": message},
    )


def pct(part: int, whole: int) -> float | None:
    return round(100 * part / whole, 1) if whole else None


class Tally:
    """Answers, mentions and ranks of one slice of answers."""

    __slots__ = ("answers", "mentioned", "ranks")

    def __init__(self):
        self.answers = 0
        self.mentioned = 0
        self.ranks: list[int] = []

    def add(self, result) -> None:
        self.answers += 1
        if result.is_mentioned:
            self.mentioned += 1
            if result.mention_rank:
                self.ranks.append(result.mention_rank)

    @property
    def visibility_pct(self) -> float | None:
        return pct(self.mentioned, self.answers)

    @property
    def avg_rank(self) -> float | None:
        return round(sum(self.ranks) / len(self.ranks), 1) if self.ranks else None


class Window:
    """The period's UTC days, its buckets and the previous window.

    Week buckets start on Monday; the first one may start before the
    window does, and only answers inside the window count.
    """

    def __init__(self, period: str, interval: str, today: date):
        days = PERIOD_DAYS[period]
        self.period, self.interval = period, interval
        self.end = today
        self.start = today - timedelta(days=days - 1)
        self.start_at = datetime.combine(self.start, time.min, tzinfo=UTC)
        self.prev_start_at = self.start_at - timedelta(days=days)
        weekly = interval == "week"
        cursor = self.start - timedelta(days=self.start.weekday()) if weekly else self.start
        step = timedelta(days=7 if weekly else 1)
        self.buckets: list[date] = []
        while cursor <= self.end:
            self.buckets.append(cursor)
            cursor += step
        self._index = {day: i for i, day in enumerate(self.buckets)}

    def bucket(self, when: datetime) -> int | None:
        day = when.astimezone(UTC).date()
        if self.interval == "week":
            day -= timedelta(days=day.weekday())
        return self._index.get(day)


def parse_filters(*, period=None, interval=None, engine=None, prompt_id=None) -> tuple:
    """(period, interval, engine, prompt uuid) from the query, defaults
    applied; raises ``invalid`` (400) for a value outside the contract."""
    period = period or "30d"
    if period not in PERIOD_DAYS:
        raise _invalid("period", "Choose a period of 7d, 30d or 90d.")
    if not interval:
        interval = "week" if period == "90d" else "day"
    elif interval not in INTERVALS:
        raise _invalid("interval", "Choose an interval of day or week.")
    engine = engine or None
    if engine is not None and engine not in ENGINE_KEYS:
        raise _invalid("engine", "Choose one of chatgpt, claude, gemini, grok or perplexity.")
    prompt_uuid = None
    if prompt_id:
        try:
            prompt_uuid = uuid.UUID(str(prompt_id))
        except ValueError:
            raise _invalid("prompt_id", "This is not a prompt id.") from None
    return period, interval, engine, prompt_uuid


def tracked_answers(website, brand_prompts: list, *, since: datetime,
                    fields: tuple = RESULT_FIELDS) -> list[tuple]:
    """(brand prompt, result) for every successful answer of the five
    engines since ``since``. A result matched to two prompts stays with
    the first (oldest) prompt only. Callers add the fields they need
    (``response_text``, ``search_queries``...) to ``fields``."""
    grouped = results_by_prompt(
        website, [bp.prompt for bp in brand_prompts], since=since, fields=fields,
    )
    seen: set = set()
    out = []
    for bp in brand_prompts:
        for result in grouped.get(bp.prompt_id, []):
            if result.id in seen or not result.query_succeeded:
                continue
            if result.provider not in ENGINE_ORDER:
                continue
            seen.add(result.id)
            out.append((bp, result))
    return out
