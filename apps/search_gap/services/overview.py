"""The search-vs-AI picture for a website, and for one tracked prompt.

Joins three things per prompt: the latest Google rank check, each AI
engine's recent answers (via ``prompt_stats``), and the citations behind
those answers. Scoring and advice are delegated to the pure modules.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timedelta

from django.db.models import F, FloatField, Sum
from django.utils import timezone

from apps.citations.services import serp_google
from apps.citations.services.url_analytics import model_key
from apps.citations.services.url_normalizer import extract_apex_domain
from apps.prompt_library.services.prompt_stats import (
    MODEL_LABELS,
    is_configured,
    matching_results,
    results_by_prompt,
)
from apps.search_gap.services import advice
from apps.search_gap.services.google_rank import is_same_site, max_depth, site_apex
from apps.search_gap.services.scoring import (
    ANSWER_WINDOW,
    Answer,
    ai_score,
    classify,
    overall,
    search_score,
)

# Display order on every screen.
ENGINE_ORDER = ("gpt4", "claude", "gemini", "grok", "perplexity")
RESULT_FIELDS = (
    "id", "public_id", "provider", "prompt", "is_mentioned", "mention_rank",
    "sentiment", "competitors_mentioned", "mention_context", "query_succeeded",
    "created_at", "source_prompt_id", "audit_id",
)
# Answers and checks older than this no longer describe the current state.
WINDOW_DAYS = 120
GSC_DAYS = 28
MAX_CITATIONS_PER_ENGINE = 8
MAX_BRANDS = 6
LEADERBOARD_SIZE = 8


def _providers() -> dict:
    from apps.llm_ranking.providers import PROVIDERS

    return {key: PROVIDERS[key] for key in ENGINE_ORDER if key in PROVIDERS}


def _engine(provider: str, results: list, configured: bool) -> tuple[dict, object]:
    """One engine's state for a prompt, plus its latest answered result."""
    answered = sorted(
        (r for r in results if r.query_succeeded),
        key=lambda r: r.created_at, reverse=True,
    )[:ANSWER_WINDOW]
    latest = answered[0] if answered else None
    mentions = [r for r in answered if r.is_mentioned]
    ranks = [r.mention_rank for r in mentions if r.mention_rank]
    view = {
        "key": model_key(provider),
        "label": MODEL_LABELS.get(provider, provider),
        "configured": configured,
        "answers": len(answered),
        "mentioned": bool(latest and latest.is_mentioned),
        "rank": latest.mention_rank if latest and latest.is_mentioned else None,
        "visibility_pct": round(100 * len(mentions) / len(answered)) if answered else None,
        "avg_rank": round(sum(ranks) / len(ranks), 1) if ranks else None,
        "score": ai_score(Answer(r.is_mentioned, r.mention_rank) for r in answered),
        "sentiment": latest.sentiment if latest and latest.is_mentioned else None,
        "last_answered_at": latest.created_at.isoformat() if latest else None,
        "latest_result_public_id": str(latest.public_id) if latest else None,
    }
    return view, latest


def _google(check) -> dict:
    # Unchecked: the depth a check would search now (ScraperAPI: the top 10).
    depth = check.depth if check else max_depth()
    base = {
        "status": "not_checked", "position": None, "page": None, "url": "",
        "title": "", "score": None, "checked_at": None, "depth": depth, "error": "",
    }
    if check is None:
        return base
    base["checked_at"] = check.checked_at.isoformat()
    if check.error:
        return {**base, "status": "error", "error": check.error}
    return {
        **base,
        "status": "ok",
        "position": check.position,
        "page": check.page,
        "url": check.url,
        "title": check.title,
        "score": search_score(check.position),
    }


def _latest_checks(website, brand_prompts) -> dict:
    """Newest successful check per prompt, else the newest failed one."""
    from apps.search_gap.models import GoogleRankCheck

    since = timezone.now() - timedelta(days=WINDOW_DAYS)
    best: dict = {}
    for check in (
        GoogleRankCheck.objects
        .filter(website=website, brand_prompt__in=brand_prompts, checked_at__gte=since)
        .order_by("-checked_at")
    ):
        current = best.get(check.brand_prompt_id)
        if current is None or (current.error and not check.error):
            best[check.brand_prompt_id] = check
    return best


def _classify_engines(engines: list[dict], google: dict) -> dict:
    search = google["score"] if google["status"] == "ok" else None
    for e in engines:
        e["class"] = classify(e["score"], search)
    gap = overall([e["score"] for e in engines], search)
    gap["by_engine"] = {e["key"]: e["class"] for e in engines}
    return gap


def _brands_above(latest, engine: dict, own_name: str) -> list[dict]:
    """Brands the answer lists ahead of you (all of them when you are absent)."""
    if latest is None:
        return []
    fold = own_name.casefold()
    rows = []
    for c in latest.competitors_mentioned or []:
        name = (c.get("name") or "").strip()
        if not name or name.casefold() == fold:
            continue
        position = c.get("position")
        if engine["mentioned"] and engine["rank"] and (not position or position >= engine["rank"]):
            continue
        rows.append({"name": name, "position": position})
    rows.sort(key=lambda b: b["position"] or 999)
    return rows[:MAX_BRANDS]


def _citations(latest_by_engine: dict, apex: str) -> dict:
    from apps.citations.models import Citation

    ids = [r.id for r in latest_by_engine.values() if r is not None]
    out: dict = defaultdict(list)
    if not ids:
        return out
    engine_of = {r.id: key for key, r in latest_by_engine.items() if r is not None}
    for c in (
        Citation.objects.filter(result_id__in=ids)
        .order_by("position")
        .values("result_id", "url", "domain", "is_target")
    ):
        rows = out[engine_of[c["result_id"]]]
        if len(rows) >= MAX_CITATIONS_PER_ENGINE or any(r["url"] == c["url"] for r in rows):
            continue
        rows.append({
            "url": c["url"],
            "domain": c["domain"],
            "is_yours": bool(c["is_target"]) or is_same_site(c["domain"], apex),
        })
    return out


def _gsc(website, query: str) -> dict | None:
    """Search Console totals for this exact query, when connected."""
    from apps.search_console.models import GscQueryStat

    since = timezone.now().date() - timedelta(days=GSC_DAYS)
    # Aliases must not reuse field names: F("impressions") inside the
    # weighted sum would resolve to an aggregate alias of the same name.
    agg = GscQueryStat.objects.filter(
        website=website, query__iexact=query.strip(), date__gte=since,
    ).aggregate(
        total_clicks=Sum("clicks"),
        total_impressions=Sum("impressions"),
        weighted_position=Sum(F("position") * F("impressions"), output_field=FloatField()),
    )
    if not agg["total_impressions"]:
        return None
    return {
        "clicks": agg["total_clicks"] or 0,
        "impressions": agg["total_impressions"],
        "avg_position": round(agg["weighted_position"] / agg["total_impressions"], 1),
        "days": GSC_DAYS,
    }


def _competitors(website) -> list[dict]:
    rows = []
    for c in website.competitors or []:
        if isinstance(c, dict) and c.get("name"):
            rows.append({"name": c["name"], "domain": c.get("domain") or ""})
        elif isinstance(c, str) and c.strip():
            rows.append({"name": c.strip(), "domain": ""})
    return rows


def _engines_for(results: list, providers: dict) -> tuple[list[dict], dict]:
    by_provider = defaultdict(list)
    for r in results:
        by_provider[r.provider].append(r)
    engines, latest = [], {}
    for provider, cls in providers.items():
        view, last = _engine(provider, by_provider.get(provider, []), is_configured(cls))
        engines.append(view)
        latest[view["key"]] = last
    return engines, latest


def _leaderboard(rows_latest: list[dict], own_name: str) -> list[dict]:
    """How often each brand appears across every prompt's latest answers."""
    self_key = "__self__"
    counts: Counter = Counter()
    by_engine: dict = defaultdict(Counter)
    display: dict = {self_key: own_name}
    answers = 0
    for latest_by_engine in rows_latest:
        for key, r in latest_by_engine.items():
            if r is None:
                continue
            answers += 1
            names = {self_key} if r.is_mentioned else set()
            for c in r.competitors_mentioned or []:
                name = (c.get("name") or "").strip()
                if name and name.casefold() != own_name.casefold():
                    fold = name.casefold()
                    display.setdefault(fold, name)
                    names.add(fold)
            for n in names:
                counts[n] += 1
                by_engine[n][key] += 1
    if not answers:
        return []
    ranked = [n for n, _ in counts.most_common(LEADERBOARD_SIZE)]
    if self_key not in ranked:
        ranked.append(self_key)
    return [
        {
            "name": display[n],
            "is_you": n == self_key,
            "visibility_pct": round(100 * counts[n] / answers),
            "by_engine": dict(by_engine[n]),
        }
        for n in ranked
    ]


def build_overview(website) -> dict:
    from apps.prompt_library.models import BrandPrompt

    providers = _providers()
    brand_prompts = list(
        BrandPrompt.objects
        .filter(website=website, is_archived=False)
        .select_related("prompt")
        .order_by("created_at")
    )
    since = timezone.now() - timedelta(days=WINDOW_DAYS)
    grouped = results_by_prompt(
        website, [bp.prompt for bp in brand_prompts], since=since, fields=RESULT_FIELDS,
    )
    checks = _latest_checks(website, brand_prompts)
    apex = site_apex(website.url)
    competitors = _competitors(website)

    rows, rows_latest = [], []
    for bp in brand_prompts:
        engines, latest = _engines_for(grouped.get(bp.prompt_id, []), providers)
        check = checks.get(bp.id)
        google = _google(check)
        google["top_results"] = check.top_results if check else []
        gap = _classify_engines(engines, google)
        # Citations are left out here (one query per prompt would not
        # scale); the list's "top fix" is a headline, the prompt page has
        # the full advice.
        actions = advice.build_actions(
            query=bp.prompt.text, site_apex=apex, google=google, engines=engines,
            competitors=competitors, overall_class=gap["class"],
        )
        google.pop("top_results")
        rows.append({
            "prompt_id": str(bp.prompt_id),
            "brand_prompt_id": str(bp.id),
            "text": bp.prompt.text,
            "location": bp.location,
            "google": google,
            "engines": {
                e["key"]: {
                    k: e[k] for k in (
                        "configured", "answers", "mentioned", "rank",
                        "visibility_pct", "score", "class",
                    )
                }
                for e in engines
            },
            "gap": gap,
            "top_action": (
                {"kind": actions[0]["kind"], "title": actions[0]["title"]} if actions else None
            ),
        })
        rows_latest.append(latest)

    classes = Counter(r["gap"]["class"] for r in rows)
    checked = [r for r in rows if r["google"]["status"] == "ok"]
    positions = [r["google"]["position"] for r in checked if r["google"]["position"]]
    visibility = [
        e["visibility_pct"] for r in rows for e in r["engines"].values()
        if e["visibility_pct"] is not None
    ]
    return {
        "website": {"id": str(website.id), "name": website.name, "url": website.url},
        "serp_configured": serp_google.is_configured(),
        "engines": [
            {"key": model_key(key), "label": MODEL_LABELS.get(key, key), "configured": is_configured(cls)}
            for key, cls in providers.items()
        ],
        "kpis": {
            "prompts": len(rows),
            "ai_visibility_pct": round(sum(visibility) / len(visibility)) if visibility else None,
            "avg_google_position": round(sum(positions) / len(positions), 1) if positions else None,
            "google_checked": len(checked),
            "not_in_google": sum(1 for r in checked if not r["google"]["position"]),
            "ai_ahead": classes.get("ai_ahead", 0),
            "google_ahead": classes.get("google_ahead", 0),
            "strong_both": classes.get("strong_both", 0),
            "invisible_both": classes.get("invisible_both", 0),
            "even": classes.get("even", 0),
            "no_data": classes.get("no_data", 0),
        },
        "prompts": sorted(rows, key=lambda r: -abs(r["gap"]["value"] or 0)),
        "leaderboard": _leaderboard(rows_latest, website.name),
    }


def _verdict(engines: list[dict], google: dict, depth: int) -> str:
    mentioned = sorted(
        (e for e in engines if e["mentioned"]), key=lambda e: e["rank"] or 999,
    )
    if mentioned:
        best = mentioned[0]
        ai = (
            f"{best['label']} lists you #{best['rank']}" if best["rank"]
            else f"{best['label']} mentions you"
        )
        others = len(mentioned) - 1
        if others:
            ai += f", and {others} other engine{'s mention' if others > 1 else ' mentions'} you"
    elif any(e["answers"] for e in engines):
        ai = "No AI engine mentions you yet"
    else:
        ai = "No AI answers yet"

    if google["status"] != "ok":
        search = "Google hasn't been checked yet"
    elif google["position"]:
        search = f"Google has you at #{google['position']} (page {google['page']})"
    else:
        search = f"Google doesn't show you in its top {depth} results"
    return f"{ai}. {search}."


def build_prompt_detail(website, brand_prompt) -> dict:
    providers = _providers()
    prompt = brand_prompt.prompt
    since = timezone.now() - timedelta(days=WINDOW_DAYS)
    results = matching_results(website, prompt, since=since, fields=RESULT_FIELDS)
    engines, latest = _engines_for(results, providers)
    apex = site_apex(website.url)
    competitors = _competitors(website)
    competitor_apexes = {
        extract_apex_domain(c["domain"]) for c in competitors if c["domain"]
    }

    check = _latest_checks(website, [brand_prompt]).get(brand_prompt.id)
    google = _google(check)
    # How a Google check for this prompt's location runs now.
    plan = serp_google.geo_plan(brand_prompt.location or "")
    google["method"] = (
        {"method": plan["method"], "label": plan["label"]} if serp_google.is_configured() else None
    )
    google["top_results"] = [
        {
            **r,
            "is_yours": is_same_site(r.get("domain", ""), apex),
            "is_competitor": extract_apex_domain(r.get("domain", "")) in competitor_apexes,
        }
        for r in (check.top_results if check else [])
    ]
    google["ai_overview"] = check.ai_overview if check else {}
    google["gsc"] = _gsc(website, prompt.text)

    citations = _citations(latest, apex)
    for e in engines:
        last = latest.get(e["key"])
        e["citations"] = citations.get(e["key"], [])
        e["brands"] = [
            {"name": (c.get("name") or "").strip(), "position": c.get("position")}
            for c in (last.competitors_mentioned if last else []) or []
            if (c.get("name") or "").strip()
        ]
        e["brands_above"] = _brands_above(last, e, website.name)
        e["snippet"] = (last.mention_context or "")[:400] if last and last.is_mentioned else ""
    gap = _classify_engines(engines, google)
    actions = advice.build_actions(
        query=prompt.text, site_apex=apex, google=google, engines=engines,
        competitors=competitors, overall_class=gap["class"],
    )
    for e in engines:
        e.pop("brands")

    return {
        "prompt": {
            "id": str(prompt.id),
            "brand_prompt_id": str(brand_prompt.id),
            "text": prompt.text,
            "location": brand_prompt.location,
            "tags": brand_prompt.tags,
        },
        "website": {"id": str(website.id), "name": website.name, "url": website.url},
        "serp_configured": serp_google.is_configured(),
        "google": google,
        "engines": engines,
        "gap": gap,
        "verdict": _verdict(engines, google, google["depth"]),
        "actions": actions,
    }
