"""GEO KPI tiles for the dashboard Analytics tab.

Builds the three top-line metrics the product surfaces:

- Visibility — share of AI answers that mention the brand
- Position   — average rank when mentioned (lower is better)
- Sentiment  — net sentiment score (0-100) across mentioned answers

Each tile compares the user's last PERIOD_DAYS of completed audits
against the PERIOD_DAYS before that, so the chart never claims a trend
on a single audit.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from django.utils import timezone

from apps.llm_ranking.models import LLMRankingAudit, LLMRankingResult

PERIOD_DAYS = 30


def build_kpis_for_user(
    user,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    prompts: list[str] | None = None,
    providers: list[str] | None = None,
    tags: list[str] | None = None,
    topics: list[str] | None = None,
    website=None,
) -> list[dict] | None:
    """Return Visibility / Position / Sentiment tiles for the given window.

    When start is None the window is unbounded on the lower side (treated
    as "overall"). When any result-level filter is provided the metrics
    are recomputed per-result — the audit-level pre-aggregates cannot be
    narrowed by prompt, provider, tag or topic. When ``website`` is given
    every metric is scoped to that project's audits only.
    """
    if not _audits_in(user, None, None, website=website).exists():
        return None

    flt = _make_flt(prompts=prompts, providers=providers, tags=tags, topics=topics)

    end = end or timezone.now()
    if start is None:
        # Default window: last PERIOD_DAYS. "Overall" callers supply
        # start explicitly (via resolve_window) so this only runs when
        # the caller passes no filter.
        start_current = end - timedelta(days=PERIOD_DAYS)
    else:
        start_current = start

    span = end - start_current
    if span.total_seconds() <= 0:
        span = timedelta(days=PERIOD_DAYS)
    current = (start_current, end)
    previous = (start_current - span, start_current)

    return [
        _build_tile(
            label="Visibility",
            subtext=(
                "See the share of chats where your brand is mentioned and "
                "understand how often you show up in conversations."
            ),
            higher_better=True,
            value_fmt=_pct,
            current=_visibility(user, *current, flt=flt, website=website),
            previous=_visibility(user, *previous, flt=flt, website=website),
        ),
        _build_tile(
            label="Position",
            subtext=(
                "Understand your brand's position within AI search results "
                "and uncover opportunities to improve your ranking."
            ),
            higher_better=False,
            value_fmt=_rank,
            current=_position(user, *current, flt=flt, website=website),
            previous=_position(user, *previous, flt=flt, website=website),
        ),
        _build_tile(
            label="Sentiment",
            subtext=(
                "Find out how your brand is perceived by AI, what's going "
                "well, and what requires improvements."
            ),
            higher_better=True,
            value_fmt=_score,
            current=_sentiment(user, *current, flt=flt, website=website),
            previous=_sentiment(user, *previous, flt=flt, website=website),
        ),
        _build_tile(
            label="Alignment",
            subtext=(
                "See how closely AI answers reflect your own brand facts "
                "and key messages, and which messages are missing."
            ),
            higher_better=True,
            value_fmt=_score,
            current=_alignment(user, *current, flt=flt, website=website),
            previous=_alignment(user, *previous, flt=flt, website=website),
        ),
    ]


# ── Breakdown payload for the per-metric detail cards ──────────────────────

def build_breakdowns_for_user(
    user,
    *,
    start=None,
    end=None,
    prompts: list[str] | None = None,
    providers: list[str] | None = None,
    tags: list[str] | None = None,
    topics: list[str] | None = None,
    website=None,
) -> dict | None:
    """Return per-metric drill-downs for the dashboard, or None if no data."""
    flt = _make_flt(prompts=prompts, providers=providers, tags=tags, topics=topics)
    end = end or timezone.now()
    if start is None:
        start = (
            _audits_in(user, None, None, website=website)
            .order_by("completed_at")
            .values_list("completed_at", flat=True)
            .first()
        )
    audit_ids = list(
        _audits_in(user, start, end, website=website).values_list("id", flat=True)
    )
    if not audit_ids:
        return None
    return {
        "visibility": _visibility_breakdown(audit_ids, flt),
        "position": _position_breakdown(audit_ids, flt),
        "sentiment": _sentiment_breakdown(audit_ids, flt),
        "alignment": _alignment_breakdown(audit_ids, flt),
    }


def _visibility_breakdown(audit_ids: list, flt: dict | None) -> dict:
    """Mention share per LLM provider in the window."""
    rows: dict[str, dict[str, int]] = {}
    for r in _filtered_results(audit_ids, flt).values("provider", "is_mentioned"):
        bucket = rows.setdefault(r["provider"], {"total": 0, "mentions": 0})
        bucket["total"] += 1
        if r["is_mentioned"]:
            bucket["mentions"] += 1
    providers = [
        {
            "provider": p,
            "mentions": v["mentions"],
            "total": v["total"],
            "mention_rate": round(100.0 * v["mentions"] / v["total"], 1) if v["total"] else 0.0,
        }
        for p, v in rows.items()
    ]
    providers.sort(key=lambda x: x["mention_rate"], reverse=True)
    return {"by_provider": providers}


def _position_breakdown(audit_ids: list, flt: dict | None) -> dict:
    """Distribution of mention rank across the window."""
    buckets = {"1": 0, "2-3": 0, "4-10": 0, "11+": 0}
    qs = (
        _filtered_results(audit_ids, flt)
        .filter(is_mentioned=True, mention_rank__isnull=False)
        .values_list("mention_rank", flat=True)
    )
    total = 0
    for rank in qs:
        total += 1
        if rank == 1:
            buckets["1"] += 1
        elif rank <= 3:
            buckets["2-3"] += 1
        elif rank <= 10:
            buckets["4-10"] += 1
        else:
            buckets["11+"] += 1
    distribution = [
        {
            "range": label,
            "count": count,
            "pct": round(100.0 * count / total, 1) if total else 0.0,
        }
        for label, count in buckets.items()
    ]
    return {"distribution": distribution, "total_mentions": total}


def _sentiment_breakdown(audit_ids: list, flt: dict | None) -> dict:
    """Positive/neutral/negative split plus a few representative quotes."""
    qs = _filtered_results(audit_ids, flt).filter(is_mentioned=True)
    counts = {"positive": 0, "neutral": 0, "negative": 0}
    for s in qs.values_list("sentiment", flat=True):
        if s in counts:
            counts[s] += 1
    total = sum(counts.values())
    split = [
        {
            "sentiment": k,
            "count": v,
            "pct": round(100.0 * v / total, 1) if total else 0.0,
        }
        for k, v in counts.items()
    ]
    samples = []
    for sentiment in (
        LLMRankingResult.SENTIMENT_POSITIVE,
        LLMRankingResult.SENTIMENT_NEGATIVE,
    ):
        row = (
            qs.filter(sentiment=sentiment)
            .exclude(mention_context="")
            .order_by("-created_at")
            .values("sentiment", "provider", "mention_context")
            .first()
        )
        if row:
            samples.append({
                "sentiment": row["sentiment"],
                "provider": row["provider"],
                "quote": row["mention_context"][:280],
            })
    return {"split": split, "samples": samples, "total_mentions": total}


def _alignment_breakdown(audit_ids: list, flt: dict | None) -> dict:
    """Aligned/partial/unaligned bands plus the brand messages AI most
    often reflects and misses — the actionable "what to publish next"
    signal, aggregated from per-result alignment detail."""
    qs = _filtered_results(audit_ids, flt).filter(alignment_score__isnull=False)
    bands = {"aligned": 0, "partial": 0, "unaligned": 0}
    reflected_counts: dict[str, int] = {}
    missing_counts: dict[str, int] = {}
    total = 0
    for score, detail in qs.values_list("alignment_score", "alignment_detail"):
        total += 1
        if score >= 70:
            bands["aligned"] += 1
        elif score >= 40:
            bands["partial"] += 1
        else:
            bands["unaligned"] += 1
        coverage = (detail or {}).get("coverage") or {}
        for item in coverage.get("reflected") or []:
            text = (item or {}).get("text") or ""
            if text:
                reflected_counts[text] = reflected_counts.get(text, 0) + 1
        for item in coverage.get("missing") or []:
            text = (item or {}).get("text") or ""
            if text:
                missing_counts[text] = missing_counts.get(text, 0) + 1

    def _top(counts: dict[str, int]) -> list[dict]:
        ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:5]
        return [{"text": text, "count": count} for text, count in ranked]

    split = [
        {
            "band": band,
            "count": count,
            "pct": round(100.0 * count / total, 1) if total else 0.0,
        }
        for band, count in bands.items()
    ]
    return {
        "split": split,
        "top_reflected": _top(reflected_counts),
        "top_missing": _top(missing_counts),
        "total_scored": total,
    }


# ── metric calculators ─────────────────────────────────────────────────────

def _audits_in(user, start, end, website=None):
    # Audits are scoped by project, not creator — teammates share dashboards.
    if website is not None:
        qs = LLMRankingAudit.objects.filter(
            website=website, status=LLMRankingAudit.STATUS_COMPLETED,
        )
    else:
        from apps.websites.services.website_service import WebsiteService

        qs = LLMRankingAudit.objects.filter(
            website__in=WebsiteService.accessible_qs(user),
            status=LLMRankingAudit.STATUS_COMPLETED,
        )
    if start is not None:
        qs = qs.filter(completed_at__gte=start)
    if end is not None:
        qs = qs.filter(completed_at__lt=end)
    return qs


def _make_flt(*, prompts=None, providers=None, tags=None, topics=None) -> dict | None:
    """Bundle the result-level filters, or None when nothing is set.

    A single object keeps every helper in this module on identical filter
    semantics, and "is anything set" checks stay one truthiness test.
    """
    if not (prompts or providers or tags or topics):
        return None
    return {"prompts": prompts, "providers": providers, "tags": tags, "topics": topics}


def _filtered_results(audit_ids, flt):
    from apps.llm_ranking.services._window import apply_result_filters

    qs = LLMRankingResult.objects.filter(
        audit_id__in=audit_ids, query_succeeded=True,
    )
    return apply_result_filters(qs, **(flt or {}))


def _visibility(user, start, end, *, flt=None, website=None) -> float | None:
    if start is None and end is None:
        return None
    if flt:
        audit_ids = list(
            _audits_in(user, start, end, website=website).values_list("id", flat=True)
        )
        if not audit_ids:
            return None
        qs = _filtered_results(audit_ids, flt)
        total = qs.count()
        if total == 0:
            return None
        return round(100.0 * qs.filter(is_mentioned=True).count() / total, 1)
    rates = [
        r
        for r in _audits_in(user, start, end, website=website).values_list(
            "mention_rate", flat=True
        )
        if r is not None
    ]
    return round(sum(rates) / len(rates), 1) if rates else None


def _position(user, start, end, *, flt=None, website=None) -> float | None:
    if start is None and end is None:
        return None
    if flt:
        audit_ids = list(
            _audits_in(user, start, end, website=website).values_list("id", flat=True)
        )
        if not audit_ids:
            return None
        ranks = list(
            _filtered_results(audit_ids, flt)
            .filter(is_mentioned=True, mention_rank__isnull=False)
            .values_list("mention_rank", flat=True)
        )
        return round(sum(ranks) / len(ranks), 1) if ranks else None
    ranks = list(
        _audits_in(user, start, end, website=website)
        .filter(avg_mention_rank__gt=0)
        .values_list("avg_mention_rank", flat=True)
    )
    return round(sum(ranks) / len(ranks), 1) if ranks else None


def _sentiment(user, start, end, *, flt=None, website=None) -> float | None:
    if start is None and end is None:
        return None
    audit_ids = list(
        _audits_in(user, start, end, website=website).values_list("id", flat=True)
    )
    if not audit_ids:
        return None
    results = _filtered_results(audit_ids, flt).filter(is_mentioned=True)
    total = results.count()
    if total == 0:
        return None
    pos = results.filter(sentiment=LLMRankingResult.SENTIMENT_POSITIVE).count()
    neg = results.filter(sentiment=LLMRankingResult.SENTIMENT_NEGATIVE).count()
    # 100 = all mentions positive, 50 = balanced/neutral, 0 = all negative.
    net = 50 + 50 * (pos - neg) / total
    return round(max(0.0, min(100.0, net)), 1)


def _alignment(user, start, end, *, flt=None, website=None) -> float | None:
    """Mean brand-alignment score over scored results in the window.

    Always recomputed from result rows (the audit-level pre-aggregate is
    a best-effort snapshot — alignment tasks run async).
    """
    if start is None and end is None:
        return None
    audit_ids = list(
        _audits_in(user, start, end, website=website).values_list("id", flat=True)
    )
    if not audit_ids:
        return None
    scores = list(
        _filtered_results(audit_ids, flt)
        .filter(alignment_score__isnull=False)
        .values_list("alignment_score", flat=True)
    )
    return round(sum(scores) / len(scores), 1) if scores else None


# ── tile shaping ───────────────────────────────────────────────────────────

def _build_tile(*, label, subtext, higher_better, value_fmt, current, previous) -> dict:
    value = value_fmt(current)
    if current is None or previous is None or previous == 0:
        return {
            "label": label,
            "value": value,
            "change": "—",
            "direction": "up",
            "subtext": subtext,
            "trendNote": "No prior period to compare",
        }
    delta = current - previous
    if higher_better:
        direction = "up" if delta >= 0 else "down"
    else:
        direction = "up" if delta <= 0 else "down"
    pct = abs(delta) / abs(previous) * 100 if previous else 0.0
    return {
        "label": label,
        "value": value,
        "change": f"{pct:.1f}%",
        "direction": direction,
        "subtext": subtext,
        "trendNote": "Trending up" if direction == "up" else "Trending down",
    }


def _pct(v): return "—" if v is None else f"{v:.1f}%"
def _rank(v): return "—" if v is None else f"{v:.1f}"
def _score(v): return "—" if v is None else f"{v:.0f}"
