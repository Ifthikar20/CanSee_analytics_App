"""Visibility-over-time series builder for the dashboard chart.

Aggregates a user's completed LLM ranking audits into bucketed series
(day / week / month) suitable for the frontend `VisibilityChart` component.

Shape returned:
    {
        "day":   {"labels": [...], "brand": [...], "competitor": [...]},
        "week":  {"labels": [...], "brand": [...], "competitor": [...]},
        "month": {"labels": [...], "brand": [...], "competitor": [...]},
    }

If the user has zero completed audits, returns None — the frontend renders
its empty state in that case.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from django.utils import timezone

from apps.llm_ranking.models import LLMRankingAudit, LLMRankingResult

DAY_BUCKETS = 7
WEEK_BUCKETS = 8
MONTH_BUCKETS = 6
MONTH12_BUCKETS = 12


def _scoped_audits(user, website=None):
    # Audits are scoped by project, not creator — teammates share dashboards.
    if website is not None:
        return LLMRankingAudit.objects.filter(
            website=website, status=LLMRankingAudit.STATUS_COMPLETED,
        )
    from apps.websites.services.website_service import WebsiteService

    return LLMRankingAudit.objects.filter(
        website__in=WebsiteService.accessible_qs(user),
        status=LLMRankingAudit.STATUS_COMPLETED,
    )


def build_for_user(user, website=None) -> dict | None:
    """Return the bundled visibility series for a user, or None if no data.

    ``website`` scopes every bucket to that project's audits so the chart
    never blends two brands' visibility into one line.
    """
    if not _scoped_audits(user, website).exists():
        return None
    now = timezone.now()
    return {
        "day": _day_series(user, now, website=website),
        "week": _week_series(user, now, website=website),
        "month": _month_series(user, now, website=website),
        "month12": _month12_series(user, now, website=website),
    }


TOP_COMPETITORS_LIMIT = 5


def build_overview_for_website(user, website) -> dict:
    """Return the Visibility Overview payload for a single website.

    Builds a 12-month brand series, a *real* per-competitor breakdown across
    the top tracked competitors, and a "competitor avg" line that is the
    monthly mean of the per-competitor rates (not "share of answers naming
    any competitor"). Includes computed headline values and trend deltas so
    the dashboard doesn't derive them client-side.

    Delta formula: mean of the last three months vs the prior three months,
    expressed as a percentage change of the prior window. Falls back to a
    first-vs-last calculation when the series has fewer than six populated
    points. ``has_data`` is False when no month was measured; current
    values and deltas are None when there is nothing to compute them from.

    Competitor selection: the top ``TOP_COMPETITORS_LIMIT`` competitors by
    total mention count over the full 12-month window. Picking the set up
    front (rather than per-bucket) keeps each per-competitor line continuous
    across months even when a given competitor wasn't mentioned in every
    bucket.
    """
    now = timezone.now()
    months = _last_n_month_starts(now.date(), MONTH12_BUCKETS)
    labels = [m.strftime("%b") for m in months]
    ranges = [
        (_start_of_day(m), _start_of_day(_first_of_next_month(m)))
        for m in months
    ]

    # Walk each month once, collecting brand rate + per-competitor breakdown
    # in a single pass so we don't query the audit table N + N×K times.
    brand: list[float | None] = []
    breakdowns: list[dict[str, float]] = []
    measured_mask: list[bool] = []
    for start, end in ranges:
        qs = _scoped_audits(user, website).filter(
            completed_at__gte=start, completed_at__lt=end,
        )
        audit_ids = list(qs.values_list("id", flat=True))
        # Track which buckets were actually measured so competitor series
        # can carry the same gaps the brand series does.
        measured_mask.append(bool(audit_ids))
        brand.append(_brand_rate(audit_ids))
        breakdowns.append(_competitor_breakdown(audit_ids))

    # Rank competitors by their summed mention rate across the window, take
    # the top N. Names are kept in their original casing for display.
    totals: dict[str, float] = {}
    for breakdown in breakdowns:
        for name, rate in breakdown.items():
            totals[name] = totals.get(name, 0.0) + rate
    top_names = sorted(totals, key=lambda n: totals[n], reverse=True)[:TOP_COMPETITORS_LIMIT]

    competitors = []
    for name in top_names:
        # Within a measured bucket, a competitor absent from the breakdown
        # genuinely scored zero. In an unmeasured bucket it is a gap.
        series = [
            round(b.get(name, 0.0), 1) if measured else None
            for b, measured in zip(breakdowns, measured_mask, strict=False)
        ]
        competitors.append({
            "name": name,
            "series": series,
            "current": _current(series),
            "delta_pct": _trend_delta(series),
        })

    # The "Competitor Avg" line: per-bucket mean across the top-N competitor
    # rates, computed only for buckets that were measured.
    competitor_avg: list[float | None] = []
    for i in range(len(labels)):
        if not top_names or not measured_mask[i]:
            competitor_avg.append(None)
            continue
        values = [c["series"][i] for c in competitors if c["series"][i] is not None]
        competitor_avg.append(round(sum(values) / len(values), 1) if values else None)

    has_data = any(measured_mask)

    return {
        "labels": labels,
        "brand": brand,
        "competitor": competitor_avg,
        "brand_current": _current(brand),
        "competitor_current": _current(competitor_avg),
        "brand_delta_pct": _trend_delta(brand),
        "competitor_delta_pct": _trend_delta(competitor_avg),
        "competitors": competitors,
        "has_data": has_data,
        # How many buckets actually contain audits. The UI needs this to
        # decide whether a trend line is meaningful at all.
        "measured_periods": sum(1 for m in measured_mask if m),
    }


def _current(series: list[float | None]) -> float | None:
    """Most recent measured value, or None when nothing was measured."""
    for value in reversed(series or []):
        if value is not None:
            return round(value, 1)
    return None


def _trend_delta(series: list[float | None]) -> float | None:
    """Percent change of the last 3 measured buckets vs the 3 before them.

    Operates on measured buckets only. None when there is no trend to
    report: fewer than two measured points, or a zero baseline that a
    percent change cannot be taken from.
    """
    points = [v for v in (series or []) if v is not None]
    if len(points) < 2:
        return None
    if len(points) >= 6:
        tail = points[-3:]
        prev = points[-6:-3]
        tail_avg = sum(tail) / len(tail)
        prev_avg = sum(prev) / len(prev)
        if prev_avg == 0:
            return None
        return round((tail_avg - prev_avg) / prev_avg * 100, 1)
    first = points[0]
    last = points[-1]
    if first == 0:
        return None
    return round((last - first) / abs(first) * 100, 1)


# ── bucket builders ─────────────────────────────────────────────────────────

def _day_series(user, now: datetime, *, website=None) -> dict:
    today = now.date()
    buckets = [today - timedelta(days=i) for i in range(DAY_BUCKETS - 1, -1, -1)]
    labels = [d.strftime("%a") for d in buckets]
    ranges = [(_start_of_day(d), _start_of_day(d) + timedelta(days=1)) for d in buckets]
    return _series_for_ranges(user, labels, ranges, website=website)


def _week_series(user, now: datetime, *, website=None) -> dict:
    # Anchor on Monday of the current week, walk back WEEK_BUCKETS - 1 weeks.
    monday = now.date() - timedelta(days=now.weekday())
    starts = [monday - timedelta(weeks=i) for i in range(WEEK_BUCKETS - 1, -1, -1)]
    labels = [f"W{i + 1}" for i in range(WEEK_BUCKETS)]
    ranges = [(_start_of_day(s), _start_of_day(s) + timedelta(weeks=1)) for s in starts]
    return _series_for_ranges(user, labels, ranges, website=website)


def _month_series(user, now: datetime, *, website=None) -> dict:
    months = _last_n_month_starts(now.date(), MONTH_BUCKETS)
    labels = [m.strftime("%b") for m in months]
    ranges = [
        (_start_of_day(m), _start_of_day(_first_of_next_month(m)))
        for m in months
    ]
    return _series_for_ranges(user, labels, ranges, website=website)


def _month12_series(user, now: datetime, *, website=None) -> dict:
    months = _last_n_month_starts(now.date(), MONTH12_BUCKETS)
    labels = [m.strftime("%b") for m in months]
    ranges = [
        (_start_of_day(m), _start_of_day(_first_of_next_month(m)))
        for m in months
    ]
    return _series_for_ranges(user, labels, ranges, website=website)


# ── core aggregation ────────────────────────────────────────────────────────

def _series_for_ranges(
    user, labels: list[str], ranges: list[tuple[datetime, datetime]],
    *, website=None,
) -> dict:
    """Compute brand + competitor rates for each (start, end) range.

    Both rates are expressed in percent (0-100) so the chart axis stays
    consistent with the existing tooltip format. When ``website`` is given,
    the audit filter is restricted to that website only.
    """
    brand: list[float | None] = []
    competitor: list[float | None] = []
    measured = 0
    for start, end in ranges:
        qs = _scoped_audits(user, website).filter(
            completed_at__gte=start,
            completed_at__lt=end,
        )
        audit_ids = list(qs.values_list("id", flat=True))
        if audit_ids:
            measured += 1
        brand.append(_brand_rate(audit_ids))
        competitor.append(_competitor_rate(audit_ids))
    # ``measured_periods`` lets the frontend refuse to draw a trend from a
    # single point. A 12-month line built from one audit is not a trend.
    return {
        "labels": labels,
        "brand": brand,
        "competitor": competitor,
        "measured_periods": measured,
    }


def _brand_rate(audit_ids: list) -> float | None:
    """Share of answered AI queries in this bucket that mention the brand,
    in percent.

    Computed from the result rows, like ``_competitor_rate``, so per-prompt
    scans count by their answers rather than by a stored audit score.
    Returns ``None`` — not 0.0 — when the bucket has no answered query. A
    month you never measured is a gap in the series, not a month where
    the brand scored zero; charting it as zero states something false.
    """
    if not audit_ids:
        return None
    answered = LLMRankingResult.objects.filter(
        audit_id__in=audit_ids, query_succeeded=True,
    )
    total = answered.count()
    if total == 0:
        return None
    return round(100.0 * answered.filter(is_mentioned=True).count() / total, 1)


def _competitor_rate(audit_ids: list) -> float | None:
    """Share of successful AI answers in this bucket that mention any competitor.

    ``None`` for unmeasured buckets and for buckets with no answered query,
    for the same reason as ``_brand_rate``.
    """
    if not audit_ids:
        return None
    succeeded = LLMRankingResult.objects.filter(
        audit_id__in=audit_ids, query_succeeded=True,
    )
    total = succeeded.count()
    if total == 0:
        return None
    with_competitors = sum(
        1 for row in succeeded.only("competitors_mentioned")
        if _has_competitor(row.competitors_mentioned)
    )
    return round(100.0 * with_competitors / total, 1)


def _competitor_breakdown(audit_ids: list) -> dict[str, float]:
    """Per-competitor mention rate for the given bucket of audits.

    Returns ``{competitor_name: mention_rate_pct}`` where the rate is the
    share of successful AI answers in the bucket that named that specific
    competitor. Competitor names are normalised case-insensitively so
    "Mixpanel" and "mixpanel" don't fork into two series.
    """
    if not audit_ids:
        return {}
    succeeded = LLMRankingResult.objects.filter(
        audit_id__in=audit_ids, query_succeeded=True,
    ).only("competitors_mentioned")
    total = succeeded.count()
    if total == 0:
        return {}

    # answer_count[name] = number of distinct AI answers in this bucket
    # that named ``name``. Dedupe within a single answer so the same
    # competitor appearing twice in one response still only counts once.
    answer_count: dict[str, int] = {}
    for row in succeeded:
        names_in_answer: set[str] = set()
        for raw in _iter_competitor_names(row.competitors_mentioned):
            key = raw.casefold()
            if key and key not in names_in_answer:
                names_in_answer.add(key)
                answer_count[raw] = answer_count.get(raw, 0) + 1
    return {
        name: round(100.0 * count / total, 1)
        for name, count in answer_count.items()
    }


def _iter_competitor_names(value):
    """Yield string names from a ``competitors_mentioned`` field value.

    The field is loosely typed in the database: sometimes a list of strings,
    sometimes a list of ``{"name": ...}`` dicts. This normalises both.
    """
    if not value or not isinstance(value, list):
        return
    for item in value:
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            name = str(item.get("name", "")).strip()
        else:
            continue
        if name:
            yield name


def _has_competitor(value) -> bool:
    """Treat any non-empty competitors_mentioned entry as 'mentions a competitor'."""
    if not value:
        return False
    if isinstance(value, list):
        return any(
            (isinstance(item, str) and item.strip())
            or (isinstance(item, dict) and item.get("name"))
            for item in value
        )
    return False


# ── date helpers ────────────────────────────────────────────────────────────

def _start_of_day(d: date) -> datetime:
    return timezone.make_aware(datetime(d.year, d.month, d.day))


def _first_of_month(d: date) -> date:
    return date(d.year, d.month, 1)


def _first_of_next_month(d: date) -> date:
    if d.month == 12:
        return date(d.year + 1, 1, 1)
    return date(d.year, d.month + 1, 1)


def _last_n_month_starts(today: date, n: int) -> list[date]:
    current = _first_of_month(today)
    months: list[date] = []
    for _ in range(n):
        months.append(current)
        # Step back one month.
        if current.month == 1:
            current = date(current.year - 1, 12, 1)
        else:
            current = date(current.year, current.month - 1, 1)
    return list(reversed(months))
