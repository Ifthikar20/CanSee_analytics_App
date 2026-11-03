"""Context providers: everything the assistant can look at.

Each provider covers one slice of the product and returns plain-text
lines for the prompt. They exist separately (rather than as one big fact
dump) for two reasons:

1. Budget. Dumping every subsystem into every question would blow the
   context window and bury the relevant numbers in noise.
2. Relevance. ``select_providers`` scores providers against the user's
   question, so "how are my prompts doing?" pulls prompt metrics rather
   than device breakdowns.

TENANT RULE: every provider takes the already-resolved (user, website)
and filters on them. No provider accepts an id from the caller's input,
and none iterates outside the given website.

A provider returns its data lines, or a single sentence (a ``str``)
saying the slice has no data yet. Only data lines count as grounding.

Every provider is best-effort: an exception yields no lines rather than
failing the answer.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger("apps")

# Per-section line caps keep any one subsystem from crowding the prompt.
_MAX_ROWS = 12


def _fmt_pct(value) -> str:
    try:
        return f"{float(value):.0f}%"
    except (TypeError, ValueError):
        return "n/a"


def _fmt_value(value) -> str:
    return "n/a" if value is None else str(value)


# ── Traffic ──────────────────────────────────────────────────────────

def traffic_lines(user, website) -> list[str] | str:
    from apps.analytics.services.analytics_service import AnalyticsService

    wid = str(website.id)
    out: list[str] = []
    ov = AnalyticsService.get_overview(website_id=wid, period="30d")
    visitors = ov.get("total_visitors") or 0
    pageviews = ov.get("total_pageviews") or 0
    if not visitors and not pageviews:
        return "No visits have been recorded for this website in the last 30 days."
    growth = ov.get("visitor_growth_pct")
    out.append(
        f"Last 30 days: {visitors} visitors, {pageviews} pageviews"
        + (
            f", visitor growth {growth}% vs the previous 30 days."
            if growth is not None
            else "; no previous period to compare growth against."
        )
    )
    day = AnalyticsService.get_overview(website_id=wid, period="24h")
    out.append(
        f"Last 24 hours: {day.get('total_visitors', 0)} visitors, "
        f"{day.get('total_pageviews', 0)} pageviews."
    )
    pages = AnalyticsService.get_top_pages(website_id=wid, period="30d", limit=5) or []
    for p in pages[:5]:
        if isinstance(p, dict):
            out.append(
                f"- Top page {p.get('url') or p.get('path') or '?'}: "
                f"{p.get('views') or p.get('count') or 0} views"
            )
    sources = AnalyticsService.get_traffic_sources(website_id=wid, period="30d") or []
    for s in sources[:5]:
        if isinstance(s, dict):
            out.append(
                f"- Traffic source {s.get('source') or '?'}: "
                f"{s.get('sessions') or s.get('count') or 0} sessions"
            )
    ai = AnalyticsService.get_ai_traffic_summary(website_id=wid, period="30d") or {}
    if ai:
        out.append(f"AI-assistant referred sessions (30d): {ai.get('total', 0)}.")
    return out


# ── AI visibility ────────────────────────────────────────────────────

def visibility_lines(user, website) -> list[str] | str:
    from apps.llm_ranking.services import visibility_series

    ov = visibility_series.build_overview_for_website(user, website) or {}
    if not ov.get("has_data"):
        return "No completed visibility audit yet, so there is no trend to report."
    delta = ov.get("brand_delta_pct")
    return [
        f"Brand visibility now: {_fmt_pct(ov.get('brand_current'))}"
        + (f" (change {delta}%)." if delta is not None else " (no earlier months to compare)."),
        f"Competitor average: {_fmt_pct(ov.get('competitor_current'))}.",
    ]


# ── Per-prompt metrics (the 'how are my prompts doing' answer) ───────

def prompt_metrics_lines(user, website) -> list[str] | str:
    """Per-prompt visibility computed from the stored audit answers.

    This is the metric users mean by "how are my prompts doing": for each
    saved prompt, how often did the models mention us, at what rank, with
    what sentiment.
    """
    from collections import defaultdict

    from apps.llm_ranking.models import LLMRankingResult

    rows = (
        LLMRankingResult.objects
        .filter(audit__website=website, audit__created_by=user, query_succeeded=True)
        .only("prompt", "provider", "is_mentioned", "mention_rank", "sentiment")
        .order_by("-created_at")[:400]
    )
    agg: dict[str, dict] = defaultdict(
        lambda: {"runs": 0, "hits": 0, "ranks": [], "providers": set(), "sentiments": []}
    )
    for r in rows:
        key = (r.prompt or "").strip()
        if not key:
            continue
        a = agg[key]
        a["runs"] += 1
        if r.is_mentioned:
            a["hits"] += 1
        if r.mention_rank:
            a["ranks"].append(r.mention_rank)
        if r.provider:
            a["providers"].add(r.provider)
        if r.sentiment:
            a["sentiments"].append(r.sentiment)
    if not agg:
        return "No audit answers stored yet, so per-prompt metrics are unavailable."

    ranked = sorted(
        agg.items(), key=lambda kv: (kv[1]["hits"] / max(kv[1]["runs"], 1)), reverse=True,
    )
    out = [f"Per-prompt visibility across {len(agg)} prompt(s) with stored answers:"]
    for prompt, a in ranked[:_MAX_ROWS]:
        rate = 100.0 * a["hits"] / max(a["runs"], 1)
        avg_rank = (sum(a["ranks"]) / len(a["ranks"])) if a["ranks"] else None
        bits = [f"mentioned in {rate:.0f}% of {a['runs']} answer(s)"]
        if avg_rank:
            bits.append(f"avg rank {avg_rank:.1f}")
        if a["providers"]:
            bits.append("models: " + ", ".join(sorted(a["providers"])))
        out.append(f'- "{prompt[:110]}" — ' + "; ".join(bits) + ".")
    weakest = [p for p, a in ranked if a["hits"] == 0]
    if weakest:
        out.append(
            f"{len(weakest)} prompt(s) never surfaced the brand, e.g. "
            + "; ".join(f'"{p[:70]}"' for p in weakest[:3]) + "."
        )
    return out


# ── Saved prompt library ─────────────────────────────────────────────

def prompts_lines(user, website) -> list[str] | str:
    from apps.prompt_library.models import BrandPrompt

    rows = list(
        BrandPrompt.objects
        .filter(website=website)
        .select_related("prompt")
        .order_by("-created_at")[:_MAX_ROWS]
    )
    if not rows:
        return "The saved prompt library for this website is empty."
    active = [r for r in rows if not r.is_archived]
    out = [f"Saved prompts ({len(active)} active), most recent first:"]
    for bp in rows:
        text = (getattr(bp.prompt, "text", "") or "").strip()
        if not text:
            continue
        flags = " [archived]" if bp.is_archived else ""
        tags = f" tags: {', '.join(bp.tags)}" if bp.tags else ""
        added = bp.created_at.strftime("%Y-%m-%d") if bp.created_at else ""
        out.append(f'- "{text[:110]}"{flags} (added {added}{tags})')
    return out


# ── Search Console insights ──────────────────────────────────────────

def search_insights_lines(user, website) -> list[str] | str:
    """Google Search Console: what people actually searched to find them."""
    from apps.search_console.models import GscDailyTotal, GscPageStat, GscQueryStat

    out: list[str] = []
    totals = list(GscDailyTotal.objects.filter(website=website).order_by("-date")[:28])
    if totals:
        clicks = sum(t.clicks for t in totals)
        impressions = sum(t.impressions for t in totals)
        avg_pos = sum(t.position for t in totals) / len(totals)
        out.append(
            f"Search Console (last {len(totals)} day(s)): {clicks} clicks, "
            f"{impressions} impressions, average position {avg_pos:.1f}."
        )
    queries = list(
        GscQueryStat.objects.filter(website=website).order_by("-clicks")[:_MAX_ROWS]
    )
    for q in queries:
        out.append(
            f'- Query "{q.query[:80]}": {q.clicks} clicks, {q.impressions} impressions, '
            f"position {q.position:.1f}"
        )
    pages = list(GscPageStat.objects.filter(website=website).order_by("-clicks")[:5])
    for p in pages:
        out.append(f"- Landing page {p.page[:90]}: {p.clicks} clicks")
    if not out:
        return "Google Search Console is not connected (or has no data yet) for this website."
    return out


# ── Brand security ───────────────────────────────────────────────────

def security_lines(user, website) -> list[str] | str:
    from apps.brand_vault.models import SafetyAlert
    from apps.llm_ranking.models import LLMRankingAudit

    qs = SafetyAlert.objects.filter(website=website)
    open_qs = qs.filter(status=SafetyAlert.STATUS_OPEN)
    if not open_qs.exists():
        # Findings come from stored answers; with none checked, "no open
        # findings" would read as a clean result that was never measured.
        checked = qs.exists() or LLMRankingAudit.objects.filter(
            website=website,
            status=LLMRankingAudit.STATUS_COMPLETED,
            results__query_succeeded=True,
        ).exists()
        if not checked:
            return "Brand security has not checked any AI answers for this website yet."
        return ["No open brand-security findings for this website."]
    by_sev = {s: open_qs.filter(severity=s).count() for s in ("high", "medium", "low")}
    out = [
        "Open brand-security findings: "
        + ", ".join(f"{n} {s}" for s, n in by_sev.items() if n)
        + "."
    ]
    for a in open_qs.order_by("-last_seen_at")[:6]:
        out.append(
            f"- {a.reference} ({a.severity}, {a.get_issue_display()}) on "
            f"{a.model or 'an AI model'}: {(a.detail or a.snippet or '')[:160]}"
        )
    return out


# ── Citations / sources ──────────────────────────────────────────────

def citations_lines(user, website) -> list[str] | str:
    from apps.citations.models import SourceInfluenceSnapshot

    snap = (
        SourceInfluenceSnapshot.objects
        .filter(website=website).order_by("-period_end").first()
    )
    if snap is None:
        return "No citation data captured yet for this website."
    out = [
        f"Citations ({snap.provider or 'all providers'}, period ending "
        f"{snap.period_end}): {snap.total_citations} total."
    ]
    for d in (snap.top_domains or [])[:8]:
        if isinstance(d, dict):
            out.append(
                f"- {d.get('domain') or d.get('name') or '?'}: "
                f"{d.get('count') or d.get('citations') or ''}"
            )
    return out


# ── Audits ───────────────────────────────────────────────────────────

def audits_lines(user, website) -> list[str] | str:
    from apps.llm_ranking.models import LLMRankingAudit

    rows = list(
        LLMRankingAudit.objects
        .filter(website=website, created_by=user)
        .order_by("-created_at")[:6]
    )
    if not rows:
        return "No visibility audits have been run for this website."
    out = [f"Recent audits ({LLMRankingAudit.objects.filter(website=website).count()} total):"]
    for a in rows:
        out.append(
            f"- {a.created_at:%Y-%m-%d %H:%M} status={a.status} "
            f"score={_fmt_value(getattr(a, 'overall_score', None))} "
            f"mention_rate={_fmt_value(getattr(a, 'mention_rate', None))} "
            f"results={a.results.count()}"
        )
    return out


# ── AI usage / billing ───────────────────────────────────────────────

def usage_lines(user, website) -> list[str]:
    from apps.metering.services.usage_reader import get_period_usage

    usage = get_period_usage(user) or {}
    totals = usage.get("totals", {}) or {}
    allowance = usage.get("allowance", {}) or {}
    out = [
        f"AI usage this billing period: {totals.get('total_tokens', 0)} tokens "
        f"across {totals.get('calls', 0)} calls, "
        f"${totals.get('estimated_cost_usd', 0)} spent."
    ]
    if allowance:
        out.append(
            f"Allowance: ${allowance.get('spent_usd', 0)} of "
            f"${allowance.get('cap_usd', 0)} ({allowance.get('plan', '')} plan)."
        )
    return out


# ── Knowledge base ───────────────────────────────────────────────────

def knowledge_lines(user, website) -> list[str] | str:
    from apps.rag.models import KnowledgeSource
    from apps.rag.services.ingest_service import pending_chunks

    # Website-scoped: the knowledge base belongs to the project, not the
    # person who added it, so the assistant reports the shared corpus.
    qs = KnowledgeSource.objects.filter(website=website)
    total = qs.count()
    if not total:
        return "The knowledge base for this website is empty."
    by_app: dict[str, int] = {}
    for src in qs.only("source_app"):
        by_app[src.source_app] = by_app.get(src.source_app, 0) + 1
    out = [
        f"Knowledge base: {total} source(s) — "
        + ", ".join(f"{k}: {v}" for k, v in sorted(by_app.items())) + "."
    ]
    pending = pending_chunks(website_id=website.id).count()
    if pending:
        out.append(
            f"{pending} knowledge-base passage(s) have no embedding yet, so "
            "search cannot reach them."
        )
    return out


@dataclass(frozen=True)
class Section:
    label: str
    lines: list[str]
    # False when the provider only said the slice has no data yet.
    has_data: bool


@dataclass(frozen=True)
class ContextProvider:
    key: str
    label: str
    fn: object
    keywords: tuple[str, ...] = field(default_factory=tuple)
    default: bool = False


# Order matters only for output readability.
PROVIDERS: tuple[ContextProvider, ...] = (
    ContextProvider("traffic", "TRAFFIC", traffic_lines, default=True, keywords=(
        "traffic", "visitor", "visits", "pageview", "page view", "session",
        "bounce", "audience", "referral", "source", "channel", "analytics",
        "top page", "landing",
    )),
    ContextProvider("visibility", "AI VISIBILITY", visibility_lines, default=True, keywords=(
        "visibility", "visible", "mention", "rank", "ranking", "share of voice",
        "competitor", "chatgpt", "claude", "gemini", "perplexity", "ai search",
        "trend", "trending",
    )),
    # NB: no bare "doing" here — it made a vague "how am I doing?" route to
    # prompt metrics instead of the account overview.
    ContextProvider("prompt_metrics", "PROMPT PERFORMANCE", prompt_metrics_lines, keywords=(
        "prompt", "prompts", "performing", "performance", "best prompt",
        "worst prompt", "which prompt", "prompt metric",
    )),
    ContextProvider("prompts", "SAVED PROMPTS", prompts_lines, keywords=(
        "prompt", "prompts", "library", "saved", "tracking", "archived", "tag",
    )),
    ContextProvider("search_insights", "SEARCH CONSOLE", search_insights_lines, keywords=(
        "search console", "gsc", "google", "search insight", "keyword", "query",
        "queries", "impression", "click", "ctr", "seo", "organic", "serp",
    )),
    ContextProvider("security", "BRAND SECURITY", security_lines, default=True, keywords=(
        "security", "alert", "finding", "hallucination", "risk", "safety",
        "misrepresent", "privacy", "leak", "reputation", "sentiment",
    )),
    ContextProvider("citations", "CITATIONS", citations_lines, keywords=(
        "citation", "cited", "source", "domain", "reference", "influence",
    )),
    ContextProvider("audits", "AUDITS", audits_lines, keywords=(
        "audit", "scan", "run", "last run", "history", "score",
    )),
    ContextProvider("usage", "AI USAGE", usage_lines, default=True, keywords=(
        "usage", "token", "cost", "spend", "bill", "billing", "plan",
        "allowance", "quota", "credit",
    )),
    ContextProvider("knowledge", "KNOWLEDGE BASE", knowledge_lines, keywords=(
        "knowledge", "brand input", "ingest", "corpus", "document", "upload",
    )),
)

PROVIDER_INDEX = {p.key: p for p in PROVIDERS}


def select_providers(question: str) -> list[ContextProvider]:
    """Pick the providers worth loading for this question.

    Keyword scoring, not an LLM call: it must be cheap enough to run on
    every message. When nothing matches (a vague "how am I doing?"), fall
    back to the default overview set.
    """
    q = (question or "").lower()
    scored: list[tuple[int, ContextProvider]] = []
    for p in PROVIDERS:
        hits = sum(1 for kw in p.keywords if kw in q)
        if hits:
            scored.append((hits, p))
    if not scored:
        return [p for p in PROVIDERS if p.default]
    scored.sort(key=lambda t: t[0], reverse=True)
    # Cap breadth so one keyword-rich question cannot pull everything.
    return [p for _, p in scored[:5]]


def build_sections(user, website, question: str) -> list[Section]:
    """Run the selected providers. A failed provider adds no section."""
    sections: list[Section] = []
    for provider in select_providers(question):
        try:
            out = provider.fn(user, website)
        except Exception:
            logger.warning(
                "assistant provider %s failed for website %s",
                provider.key, getattr(website, "id", None), exc_info=True,
            )
            continue
        if isinstance(out, str):
            sections.append(Section(provider.label, [out], has_data=False))
        elif out:
            sections.append(Section(provider.label, list(out), has_data=True))
    return sections
