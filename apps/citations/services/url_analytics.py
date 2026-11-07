"""Aggregation helpers for the Sources > URLs dashboard.

The dashboard surfaces, per website, how often each cited URL is *retrieved*
by the LLMs (one ``Citation`` row == one retrieval) plus derived analytics:
URL-type / domain-type classification, per-model breakdowns, movers
(Top/New/Trending/Losing), a daily retrievals time-series and a per-URL
detail view (prompts, brands, recent chats).

All counting is done in Python over a single prefetched queryset because the
per-website citation volume is modest and the derived fields (url_type,
domain_type, gap_score) are not stored on the row.

Defined metrics (documented here so the frontend/product stay in sync):

* retrievals      -- number of Citation rows for a URL in the window (the
                     page appeared as a candidate source this many times).
* citations       -- sum of reference_count: how many times the URL was
                     explicitly referenced in answer text (a subset signal of
                     retrievals).
* prompts         -- distinct prompts whose answer retrieved the URL.
* citation_rate   -- cited share: retrievals whose answer explicitly cited
                     the URL (reference_count > 0) / retrievals. Bounded 0-1,
                     rounded to 2 dp, rendered as a percentage in the UI.
* gap_score       -- (distinct tracked competitors appearing in answers that
                     used the URL) x retrievals (Peec source-gap priority).
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import date, timedelta
from urllib.parse import urlsplit

from django.db.models import F

from apps.citations.models import Citation, SourceClass

# ── source_class -> dashboard domain-type bucket ──────────────────────────
_DOMAIN_TYPE_BY_CLASS = {
    SourceClass.REDDIT: "UGC",
    SourceClass.QUORA: "UGC",
    SourceClass.STACKOVERFLOW: "UGC",
    SourceClass.FORUM: "UGC",
    SourceClass.SOCIAL: "UGC",
    SourceClass.YOUTUBE: "UGC",
    SourceClass.PODCAST: "UGC",
    SourceClass.NEWS: "Editorial",
    SourceClass.BLOG: "Editorial",
    SourceClass.WIKIPEDIA: "Reference",
    SourceClass.DOCS: "Reference",
    SourceClass.GOV: "Reference",
    SourceClass.EDU: "Reference",
    # Ownership classes get their own buckets: collapsing them into
    # "Corporate" hid the one distinction the reader cares most about.
    SourceClass.YOUR_SITE: "Your site",
    SourceClass.COMPETITOR_SITE: "Competitor",
    SourceClass.OTHER: "Reference",
}

# Stable colour palette so the overview chart legend matches the frontend.
_DOMAIN_COLORS = [
    "#475569", "#8b5cf6", "#5b8def", "#ec4899", "#f59e0b",
    "#22c55e", "#06b6d4", "#f97316", "#14b8a6", "#eab308",
]
_URL_TYPE_COLORS = {
    "Listicle": "#5b8def",
    "Other": "#94a3b8",
    "Article": "#22c55e",
    "Product Page": "#8b5cf6",
    "Comparison": "#06b6d4",
    "Homepage": "#f97316",
    "How-To Guide": "#14b8a6",
    "Category Page": "#eab308",
}

# Backend provider id -> frontend model key used by SourcesUrlsPage MODELS map.
_MODEL_KEY = {
    "claude": "claude",
    "gpt4": "chatgpt",
    "gemini": "gemini",
    "perplexity": "perplexity",
    "grok": "grok",
    "deepseek": "deepseek",
    "mistral": "mistral",
    "cohere": "cohere",
    "meta_llama": "llama",
    "amazon_nova": "nova",
}


def model_key(provider: str) -> str:
    return _MODEL_KEY.get(provider, provider or "other")


def domain_type_for(source_class: str) -> str:
    return _DOMAIN_TYPE_BY_CLASS.get(source_class, "Reference")


def topic_of(result) -> str | None:
    """The dashboard "topic" for a result == the source prompt's industry name.

    This is the prompt bundle the user organises prompts under on the Prompts
    page (e.g. "ALi"). Results whose prompt text could not be linked back to a
    library Prompt have no topic.
    """
    sp = getattr(result, "source_prompt", None)
    if sp is not None and getattr(sp, "industry_id", None):
        return sp.industry.name
    return None



def classify_url_type(url: str, title: str = "") -> str:
    """Heuristic page-type classifier from the URL path + title."""
    try:
        parts = urlsplit(url)
    except ValueError:
        parts = None
    path = (parts.path if parts else "").lower()
    clean = path.strip("/")
    first_seg = clean.split("/", 1)[0] if clean else ""
    t = (title or "").lower()
    blob = f"{clean.replace('-', ' ').replace('/', ' ')} {t}"

    if not clean:
        return "Homepage"
    if any(k in blob for k in (" vs ", "vs.", "compare", "comparison", "versus")):
        return "Comparison"
    if "how to" in blob or "how-to" in path:
        return "How-To Guide"
    # "10 best", "top 7", "5 apps" — leading-number listicles.
    if first_seg[:2].strip("-").isdigit() or t.split(" ", 1)[0].isdigit() or any(
        k in blob for k in ("best ", "top ", " best", " top ")
    ):
        return "Listicle"
    if any(k in path for k in ("/category", "/categories", "/topics", "/tag/")):
        return "Category Page"
    if any(k in path for k in ("/product", "/pricing", "/features")):
        return "Product Page"
    if any(k in path for k in ("/blog", "/article", "/news", "/post")):
        return "Article"
    return "Other"


def _prev_window(start: date, end: date) -> tuple[date, date]:
    span = (end - start).days or 1
    return start - timedelta(days=span), start - timedelta(days=1)


def tracked_competitors(website) -> set[str]:
    """Lower-cased names of the website's tracked competitors."""
    out: set[str] = set()
    for comp in (getattr(website, "competitors", None) or []):
        if isinstance(comp, dict):
            name = (comp.get("name") or "").strip().lower()
        else:
            name = str(comp).strip().lower()
        if name:
            out.add(name)
    return out


def _aggregate_rows(citations, competitors: set[str] | None = None) -> dict[str, dict]:
    """Group citations by normalized_url into per-URL accumulators.

    ``competitors`` is the set of tracked competitor names (lower-cased) used
    to compute the gap score.
    """
    competitors = competitors or set()
    by_url: dict[str, dict] = {}
    for c in citations:
        key = c.normalized_url or c.url
        agg = by_url.get(key)
        if agg is None:
            agg = by_url[key] = {
                "normalized_url": key,
                "url": c.url,
                "title": c.title or "",
                "domain": c.domain,
                "apex_domain": c.apex_domain,
                "source_class": c.source_class,
                "retrievals": 0,
                "references": 0,
                "cited_retrievals": 0,
                "prompts": set(),
                "providers": set(),
                "positions": [],
                "competitors": set(),
                "is_target": False,
                "is_competitor": False,
                "first_seen": c.created_at,
                "last_seen": c.created_at,
            }
        agg["retrievals"] += 1
        agg["references"] += getattr(c, "reference_count", 0) or 0
        if (getattr(c, "reference_count", 0) or 0) > 0:
            agg["cited_retrievals"] += 1
        if not agg["title"] and c.title:
            agg["title"] = c.title
        result = c.result
        agg["prompts"].add((result.prompt_index, result.prompt))
        agg["providers"].add(result.provider)
        if c.position is not None:
            agg["positions"].append(c.position)
        agg["is_target"] = agg["is_target"] or c.is_target
        agg["is_competitor"] = agg["is_competitor"] or c.is_competitor
        if c.created_at < agg["first_seen"]:
            agg["first_seen"] = c.created_at
        if c.created_at > agg["last_seen"]:
            agg["last_seen"] = c.created_at
        # Gap: which tracked competitors appear in answers that used this URL.
        for comp in (result.competitors_mentioned or []):
            name = (comp.get("name") if isinstance(comp, dict) else str(comp)) or ""
            name = name.strip().lower()
            if name and name in competitors:
                agg["competitors"].add(name)
    return by_url


def _row_payload(agg: dict) -> dict:
    retrievals = agg["retrievals"]
    references = agg["references"]
    # Citation rate = cited share: of the times this URL was retrieved,
    # how often the answer explicitly cited it. Bounded 0-1 so the UI can
    # render a plain percentage.
    citation_rate = (
        round(agg["cited_retrievals"] / retrievals, 2) if retrievals else 0.0
    )
    # Gap score (Peec): tracked competitors present in the source x usage.
    gap_score = len(agg["competitors"]) * retrievals
    parts = urlsplit(agg["url"])
    path = f"{parts.netloc}{parts.path}".rstrip("/")
    return {
        "url": agg["url"],
        "normalized_url": agg["normalized_url"],
        "title": agg["title"] or agg["apex_domain"],
        "path": path or agg["apex_domain"],
        "domain": agg["domain"],
        "apex_domain": agg["apex_domain"],
        "url_type": classify_url_type(agg["url"], agg["title"]),
        "domain_type": domain_type_for(agg["source_class"]),
        "source_class": agg["source_class"],
        "models": sorted({model_key(p) for p in agg["providers"]}),
        "retrievals": retrievals,
        "citations": references,
        "prompts": len(agg["prompts"]),
        "citation_rate": citation_rate,
        "gap_score": gap_score,
        "competitor_names": sorted(agg["competitors"]),
        "is_target": agg["is_target"],
        "is_competitor": agg["is_competitor"],
        "first_seen": agg["first_seen"].isoformat(),
        "last_seen": agg["last_seen"].isoformat(),
    }


def build_urls_overview(website, *, start: date, end: date, provider: str | None = None,
                        topic: str | None = None) -> dict:
    """Full payload for the URLs list view, optionally scoped to one topic."""
    qs = (
        Citation.objects.filter(
            audit__website=website,
            created_at__date__gte=start,
            created_at__date__lte=end,
        )
        .select_related("result", "result__source_prompt__industry")
    )
    if provider:
        qs = qs.filter(result__provider=provider)
    citations = list(qs)

    # Topics dropdown reflects every topic in the (period + provider) scope,
    # regardless of the currently selected topic, so the user can switch.
    topic_counter: Counter = Counter()
    for c in citations:
        t = topic_of(c.result)
        if t:
            topic_counter[t] += 1
    topics = [
        {"name": name, "count": count}
        for name, count in topic_counter.most_common()
    ]

    if topic:
        citations = [c for c in citations if topic_of(c.result) == topic]

    by_url = _aggregate_rows(citations, tracked_competitors(website))
    rows = [_row_payload(a) for a in by_url.values()]
    rows.sort(key=lambda r: r["retrievals"], reverse=True)

    total_retrievals = sum(r["retrievals"] for r in rows)

    # ── URL types breakdown ──
    type_counter: Counter = Counter()
    for r in rows:
        type_counter[r["url_type"]] += r["retrievals"]
    url_types = [
        {
            "type": t,
            "count": c,
            "pct": round(100 * c / total_retrievals) if total_retrievals else 0,
            "color": _URL_TYPE_COLORS.get(t, "#94a3b8"),
        }
        for t, c in type_counter.most_common()
    ]

    # ── Overview time-series: daily retrievals for the top domains ──
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    domain_totals: Counter = Counter()
    domain_daily: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for c in citations:
        apex = c.apex_domain or c.domain
        if not apex:
            continue
        domain_totals[apex] += 1
        domain_daily[apex][c.created_at.date().isoformat()] += 1
    top_domains = [d for d, _ in domain_totals.most_common(5)]
    series = [
        {
            "key": d,
            "label": d,
            "color": _DOMAIN_COLORS[i % len(_DOMAIN_COLORS)],
            "data": [domain_daily[d].get(day.isoformat(), 0) for day in days],
        }
        for i, d in enumerate(top_domains)
    ]
    overview = {
        "labels": [day.strftime("%b %d") for day in days],
        "series": series,
    }

    # ── Movers ──
    movers = _build_movers(citations, rows, start, end, website)

    return {
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "total_retrievals": total_retrievals,
        "unique_urls": len(rows),
        "topics": topics,
        "selected_topic": topic or None,
        "overview": overview,
        "movers": movers,
        "url_types": url_types,
        "urls": rows,
    }


def _build_movers(citations, rows, start: date, end: date, website=None) -> dict:
    mid = start + timedelta(days=((end - start).days + 1) // 2)
    recent_cut = end - timedelta(days=min(7, max(1, (end - start).days // 4)))

    first_seen: dict[str, date] = {}
    recent: Counter = Counter()
    prior: Counter = Counter()
    title_by_url: dict[str, str] = {}
    for c in citations:
        key = c.normalized_url or c.url
        d = c.created_at.date()
        if key not in first_seen or d < first_seen[key]:
            first_seen[key] = d
        if c.title and key not in title_by_url:
            title_by_url[key] = c.title
        if d >= mid:
            recent[key] += 1
        else:
            prior[key] += 1

    # "New" must mean new to this brand's history, not new to the query
    # window — a URL cited months ago and again yesterday is not new. One
    # bounded query over the window's keys pulls the all-time first seen.
    if website is not None and first_seen:
        from django.db.models import Min
        all_time = (
            Citation.objects
            .filter(audit__website=website, normalized_url__in=list(first_seen))
            .values("normalized_url")
            .annotate(fs=Min("created_at"))
        )
        for row in all_time:
            first_seen[row["normalized_url"]] = row["fs"].date()

    by_key = {r["normalized_url"]: r for r in rows}

    def entry(key):
        r = by_key.get(key, {})
        return {
            "url": r.get("url", key),
            "title": r.get("title") or title_by_url.get(key) or key,
            "retrievals": r.get("retrievals", 0),
        }

    top = [entry(r["normalized_url"]) for r in rows[:8]]

    new = sorted(
        (k for k, fs in first_seen.items() if fs >= recent_cut),
        key=lambda k: by_key.get(k, {}).get("retrievals", 0),
        reverse=True,
    )
    new_rows = [entry(k) for k in new[:8]]

    deltas = {k: recent[k] - prior[k] for k in set(recent) | set(prior)}
    trending = sorted(
        (k for k, dlt in deltas.items() if dlt > 0), key=lambda k: deltas[k], reverse=True
    )
    losing = sorted(
        (k for k, dlt in deltas.items() if dlt < 0), key=lambda k: deltas[k]
    )

    def with_delta(keys):
        out = []
        for k in keys[:8]:
            e = entry(k)
            e["delta"] = deltas.get(k, 0)
            out.append(e)
        return out

    return {
        "Top": top,
        "New": new_rows,
        "Trending": with_delta(trending),
        "Losing": with_delta(losing),
    }


def build_url_detail(website, *, normalized_url: str, start: date, end: date,
                     provider: str | None = None, topic: str | None = None) -> dict | None:
    """Detail payload for a single cited URL. Returns None if not found."""
    prev_start, prev_end = _prev_window(start, end)

    qs = (
        Citation.objects.filter(
            audit__website=website,
            normalized_url=normalized_url,
            created_at__date__gte=prev_start,
            created_at__date__lte=end,
        )
        .select_related("result", "result__source_prompt__industry")
    )
    if provider:
        qs = qs.filter(result__provider=provider)
    citations = list(qs)
    if topic:
        citations = [c for c in citations if topic_of(c.result) == topic]
    if not citations:
        return None

    current = [c for c in citations if c.created_at.date() >= start]
    previous = [c for c in citations if c.created_at.date() < start]
    if not current:
        current = citations  # fall back so the page still renders

    sample = current[0]
    agg = _aggregate_rows(current, tracked_competitors(website))
    row = _row_payload(next(iter(agg.values())))

    cur_retr = len(current)
    prev_retr = len(previous)
    cur_prompts = len({(c.result.prompt_index, c.result.prompt) for c in current})
    prev_prompts = len({(c.result.prompt_index, c.result.prompt) for c in previous})
    # Cited share (see module docstring): retrievals with an explicit
    # reference / all retrievals, bounded 0-1.
    cur_cited = sum(1 for c in current if (getattr(c, "reference_count", 0) or 0) > 0)
    prev_cited = sum(1 for c in previous if (getattr(c, "reference_count", 0) or 0) > 0)
    cur_rate = round(cur_cited / cur_retr, 2) if cur_retr else 0.0
    prev_rate = round(prev_cited / prev_retr, 2) if prev_retr else 0.0
    first_seen = min(c.created_at for c in citations)
    last_seen = max(c.created_at for c in citations)

    metrics = {
        "url": sample.url,
        "title": row["title"],
        "url_type": row["url_type"],
        "domain_type": row["domain_type"],
        "source_class": row["source_class"],
        "is_target": row["is_target"],
        "retrievals": {"value": cur_retr, "delta": cur_retr - prev_retr},
        "citation_rate": {"value": cur_rate, "delta": round(cur_rate - prev_rate, 2)},
        "prompts": {"value": cur_prompts, "delta": cur_prompts - prev_prompts},
        "first_seen": first_seen.isoformat(),
        "last_seen": last_seen.isoformat(),
    }

    # ── Retrievals over time (current vs previous period) ──
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    cur_daily: Counter = Counter(c.created_at.date().isoformat() for c in current)
    prev_daily: Counter = Counter(c.created_at.date().isoformat() for c in previous)
    prev_days = [prev_start + timedelta(days=i) for i in range((prev_end - prev_start).days + 1)]
    retrievals_over_time = {
        "labels": [d.strftime("%b %d") for d in days],
        "current": [cur_daily.get(d.isoformat(), 0) for d in days],
        "previous": [prev_daily.get(d.isoformat(), 0) for d in prev_days],
    }

    # ── Retrievals by model ──
    cur_by_model: Counter = Counter(model_key(c.result.provider) for c in current)
    prev_by_model: Counter = Counter(model_key(c.result.provider) for c in previous)
    models = sorted(set(cur_by_model) | set(prev_by_model))
    retrievals_by_model = [
        {
            "model": m,
            "current": cur_by_model.get(m, 0),
            "previous": prev_by_model.get(m, 0),
        }
        for m in models
    ]

    # ── Prompts table ──
    prompt_acc: dict[str, dict] = {}
    for c in current:
        r = c.result
        p = prompt_acc.setdefault(r.prompt, {
            "prompt": r.prompt,
            "topic": topic_of(r) or r.get_prompt_type_display(),
            "retrieved": 0,
            "cited": 0,
        })
        p["retrieved"] += 1
        if (getattr(c, "reference_count", 0) or 0) > 0:
            p["cited"] += 1
    prompts_table = []
    for p in sorted(prompt_acc.values(), key=lambda x: x["retrieved"], reverse=True):
        prompts_table.append({
            "prompt": p["prompt"],
            "topic": p["topic"],
            "retrieved": p["retrieved"],
            "citation_rate": round(p["cited"] / p["retrieved"], 2) if p["retrieved"] else 0.0,
        })

    # ── Brands mentioned (target + competitors across the citing answers) ──
    brand_freq: Counter = Counter()
    brand_pos: dict[str, list[int]] = defaultdict(list)
    brand_name = website.name
    for c in current:
        r = c.result
        if r.is_mentioned:
            brand_freq[brand_name] += 1
            if r.mention_rank:
                brand_pos[brand_name].append(r.mention_rank)
        for comp in (r.competitors_mentioned or []):
            name = (comp.get("name") or "").strip() if isinstance(comp, dict) else str(comp)
            if not name:
                continue
            brand_freq[name] += 1
            pos = comp.get("position") if isinstance(comp, dict) else None
            if pos:
                brand_pos[name].append(pos)
    brands_mentioned = []
    for name, freq in brand_freq.most_common():
        positions = brand_pos.get(name) or []
        brands_mentioned.append({
            "brand": name,
            "position": min(positions) if positions else None,
            "frequency": freq,
        })
    brands_mentioned.sort(key=lambda b: (b["position"] is None, b["position"] or 0))

    # ── Recent chats ──
    chats = []
    seen_results = set()
    for c in sorted(current, key=lambda c: c.created_at, reverse=True):
        r = c.result
        if r.id in seen_results:
            continue
        seen_results.add(r.id)
        other_sources = [
            oc.apex_domain for oc in current
            if oc.result_id == r.id and oc.normalized_url != normalized_url
        ]
        chats.append({
            "result_id": str(r.public_id),
            "prompt": r.prompt,
            "response_preview": (r.response_text or "")[:180],
            "brand_mentioned": r.is_mentioned,
            "models": [model_key(r.provider)],
            "sources": sorted(set(s for s in other_sources if s))[:6],
            "position": c.position,
            "created_at": r.created_at.isoformat(),
        })
        if len(chats) >= 20:
            break

    return {
        "metrics": metrics,
        "retrievals_over_time": retrievals_over_time,
        "retrievals_by_model": retrievals_by_model,
        "prompts": prompts_table,
        "brands_mentioned": brands_mentioned,
        "chats": chats,
    }


# Only numbered list items ("1.", "2)") lead with a brand. Sub-bullets
# ("- **Description**: ...") are detail lines, not brands, so we ignore
# dash/asterisk bullets entirely.
_LIST_ITEM = re.compile(r"^\s*\d+[.)]\s+(.*\S)\s*$")
_BOLD = re.compile(r"\*\*([^*\n]{1,60}?)\*\*")
_LEAD_SPLIT = re.compile(r"\s+[—:\-–]\s+|[:(]| - ")
# Markdown links hide the name from every pattern here ("[Rosalind
# Coffee](https://…)" never matched, so an answer full of linked names
# read as brand-free). Unwrapped to their label before matching.
_MD_LINK = re.compile(r"\[([^\]\n]+)\]\([^)\s]*\)")
# Card-style answers (typical of web-search output) lead each entry with
# a bold name ALONE on its own line, without list numbering.
_BOLD_ONLY_LINE = re.compile(r"^\*\*([^*\n]{2,60}?)\*\*$")
# A line that is exactly one markdown link (optionally bolded) is also an
# entry-name line ("[Rosalind Coffee](https://…)").
_PURE_LINK_LINE = re.compile(r"^\*{0,2}\[[^\]\n]+\]\([^)\s]*\)\*{0,2}$")

# Generic section labels that show up in bold but are never brand names.
_NON_BRAND = {
    "description", "overview", "summary", "note", "notes", "pros", "cons",
    "price", "pricing", "cost", "location", "locations", "address", "hours",
    "features", "highlights", "tips", "why", "rating", "ratings", "reviews",
    "specialty", "specialties", "best for", "ideal for", "details", "website",
    "phone", "contact", "menu", "products", "services", "about", "bottom line",
    "verdict", "recommendation", "key features", "what we like",
}


def extract_response_brands(text: str, limit: int = 25) -> list[dict]:
    """Pull the ranked entity/brand names a list-style answer recommends.

    Models answer "best X" prompts as a numbered list where each item leads
    with the brand name (often bold). We capture that lead name and its list
    position so the chat panel can show the brands the model named, even when
    none are tracked competitors. Sub-bullets and generic section labels
    (Description, Pros, Price, ...) are filtered out. Best-effort, heuristic.
    """
    if not text:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = _MD_LINK.sub(r"\1", raw)
        m = _LIST_ITEM.match(line)
        if m:
            item = m.group(1).strip()
        else:
            # Card-style entry: the whole line is one bold name and/or one
            # markdown link naming the entry.
            stripped = line.strip()
            solo = _BOLD_ONLY_LINE.match(stripped)
            if solo:
                candidate = solo.group(1).strip()
            elif _PURE_LINK_LINE.match(raw.strip()):
                candidate = stripped.strip("*").strip()
            else:
                continue
            # Headings ("Best places to start:") also appear as bold-only
            # lines; real brand names are short and never end with ':'.
            if candidate.endswith(":") or len(candidate.split()) > 6:
                continue
            item = f"**{candidate}**"
        bold = _BOLD.search(item)
        name = bold.group(1) if bold else _LEAD_SPLIT.split(item, 1)[0]
        name = name.strip().strip("*\"'.,:").strip()
        if not (1 < len(name) <= 60):
            continue
        key = name.lower()
        if key in seen or key in _NON_BRAND:
            continue
        seen.add(key)
        out.append({"name": name, "position": len(out) + 1})
        if len(out) >= limit:
            break
    return out


def build_chat_detail(website, *, result_id: str) -> dict | None:
    """Full conversation payload for one chat (LLMRankingResult).

    Used by the chat modal: the prompt + full answer, the brands mentioned,
    the model's fan-out sub-queries, and every source cited in the answer.
    Returns None if the result does not belong to this website.
    """
    from apps.citations.models import Citation
    from apps.llm_ranking.models import LLMRankingResult

    try:
        result = (
            LLMRankingResult.objects
            .select_related("audit", "source_prompt")
            .get(public_id=result_id, audit__website=website)
        )
    except LLMRankingResult.DoesNotExist:
        return None

    # Sources: every URL cited in this answer, in citation order.
    source_rows = (
        Citation.objects.filter(result=result)
        .order_by("position", "id")
        .only("url", "title", "apex_domain", "position", "source_class")
    )
    sources = [
        {
            "url": c.url,
            "title": c.title or c.apex_domain,
            "apex_domain": c.apex_domain,
            "position": c.position,
        }
        for c in source_rows
    ]

    # Entities named in this answer, split into real BRANDS and generic
    # INFORMATION. An entry only counts as a brand with actual evidence:
    # the extractor classified it (kind, v4+), it carries an official
    # domain, it matches a tracked competitor / the tracked brand, or its
    # name matches a domain this very answer cited. Everything else —
    # equipment, categories, concepts scraped from list headings like
    # "Golf Clubs" — renders as information, not as a competitor.
    tracked_names = {
        (c.get("name") or "").strip().lower()
        for c in (website.competitors or [])
        if isinstance(c, dict)
    }
    tracked_names.discard("")
    own_name = (website.name or "").strip().lower()
    cited_hosts = {
        (s.get("apex_domain") or "").lower()
        for s in sources
        if s.get("apex_domain")
    }

    def _matches_cited_host(name_low: str) -> bool:
        compact = name_low.replace(" ", "").replace("-", "")
        for host in cited_hosts:
            stem = host.split(".")[0]
            if name_low == host or compact == host.replace(".", ""):
                return True
            if len(stem) >= 4 and stem in compact:
                return True
        return False

    def _classify(name: str, domain: str, explicit_kind) -> str:
        if explicit_kind == "generic":
            return "info"
        if explicit_kind == "brand":
            return "brand"
        low = (name or "").strip().lower()
        if domain:
            return "brand"
        if low == own_name or low in tracked_names:
            return "brand"
        if _matches_cited_host(low):
            return "brand"
        return "info"

    brands = []
    seen_brands: set[str] = set()

    def _add_brand(name, position, domain="", explicit_kind=None):
        # Some stored entries keep the answer's list numbering ("1. Golf
        # Clubs") — strip it so chips read clean and dedupe works.
        name = re.sub(r"^\d+[.)]\s*", "", (name or "").strip())
        key = name.lower()
        if not name or key in seen_brands:
            return
        seen_brands.add(key)
        # domain (LLM-supplied) is used by the UI only to crawl the logo.
        brands.append({
            "name": name,
            "position": position,
            "domain": domain or "",
            "kind": _classify(name, domain or "", explicit_kind),
        })

    if result.is_mentioned:
        _add_brand(website.name, result.mention_rank, explicit_kind="brand")
    for comp in (result.competitors_mentioned or []):
        if isinstance(comp, dict):
            _add_brand(
                comp.get("name"), comp.get("position"), comp.get("domain"),
                comp.get("kind"),
            )
        else:
            _add_brand(str(comp), None)
    # Capture entities straight from the model's recommendation list.
    for entry in extract_response_brands(result.response_text or ""):
        _add_brand(entry["name"], entry["position"])

    # Fan-out sub-queries the model researched (per saved prompt).
    # Re-crawls append fresh PromptFanout rows, so de-dupe by text.
    fanout_queries: list[str] = []
    if result.source_prompt_id:
        from apps.prompt_library.models import PromptFanout

        seen_fanout: set[str] = set()
        for text in (
            PromptFanout.objects
            .filter(website=website, prompt_id=result.source_prompt_id)
            .order_by(F("confidence").desc(nulls_last=True), "created_at")
            .values_list("text", flat=True)
        ):
            key = (text or "").strip().lower()
            if not key or key in seen_fanout:
                continue
            seen_fanout.add(key)
            fanout_queries.append(text)
            if len(fanout_queries) >= 12:
                break

    # Country hint for the flag chip (top of the per-result breakdown).
    country = None
    cc = result.citation_countries or {}
    if isinstance(cc, dict) and cc:
        country = max(cc.items(), key=lambda kv: kv[1])[0]

    # Brand alignment block for the modal's Details aside. Null when the
    # response was never analyzed (legacy rows, feature disabled).
    alignment = None
    align_detail = getattr(result, "alignment_detail", None) or {}
    if align_detail.get("status"):
        coverage = align_detail.get("coverage") or {}
        support = align_detail.get("support") or {}
        alignment = {
            "score": result.alignment_score,
            "status": align_detail.get("status"),
            "reflected": [
                {"text": (i or {}).get("text") or ""}
                for i in (coverage.get("reflected") or [])[:5]
            ],
            "missing": [
                {"text": (i or {}).get("text") or ""}
                for i in (coverage.get("missing") or [])[:5]
            ],
            "unsupported": [
                {"text": (s or {}).get("text") or ""}
                for s in (support.get("unsupported_samples") or [])[:5]
            ],
        }

    return {
        "result_id": str(result.public_id),
        "provider": result.provider,
        "model": model_key(result.provider),
        # Exact model that answered (e.g. "gpt-5.6-terra"); "" on rows
        # predating per-prompt model selection.
        "model_id": getattr(result, "model_id", "") or "",
        "country": country,
        "prompt": result.prompt,
        "response_text": result.response_text or "",
        "brand": website.name,
        "brand_mentioned": result.is_mentioned,
        "brands": brands,
        "fanout_queries": fanout_queries,
        "sources": sources,
        "alignment": alignment,
        "created_at": result.created_at.isoformat(),
    }
