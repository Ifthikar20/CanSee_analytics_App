"""
Source Intelligence scan orchestrator.

Runs the full pipeline for one SourceScan row:
  1. SERP: top web results for the query (Perplexity index).
  2. READ: per-URL content (Reddit JSON API or SSRF-guarded page fetch).
  3. UNDERSTAND: per-result brand/sentiment extraction (cheap LLM).
  4. AGGREGATE: cross-result share of voice on the scan row.
"""

from __future__ import annotations

import logging
import math
import re
from collections import defaultdict
from datetime import date
from urllib.parse import urlparse

from django.utils import timezone

from apps.citations.models import (
    SourceScan,
    SourceScanEngineAnswer,
    SourceScanResult,
    SourceScanStatus,
)
from apps.citations.services import (
    community,
    content_reader,
    engine_probe,
    serp_google,
    source_sentiment,
    web_search,
)
from apps.citations.services.domain_classifier import _rule_classify
from apps.citations.services.url_normalizer import extract_apex_domain

logger = logging.getLogger("apps")

MAX_RESULTS_PER_SCAN = 16

# Per-lane quotas rather than one global cap. A shared cap lets whichever
# lane happens to return more crowd the other out entirely, and a scan with
# no community threads is exactly the scan this feature exists to avoid.
MAX_WEB_RESULTS = 10
MAX_COMMUNITY_RESULTS = 6

# Rank weight given to a brand an AI engine recommends. 1.0 is what a
# lane's #1 result gets: being named in the answer itself is at least as
# valuable as topping the list of links under it.
ENGINE_RANK_WEIGHT = 1.0

# Claude sometimes fabricates a plausible-sounding "issue" describing the
# fetch failure itself (e.g. "Access denied error preventing content
# review") when it's handed a blocked page as content. Since the
# extractor runs after fetch validation guards this shouldn't normally
# happen, but the model is unreliable enough that we filter as a belt.
# UI mirrors this regex for older scans already in the DB.
_JUNK_ISSUE_RE = re.compile(
    r"access\s+denied|preventing\s+content|content\s+review|"
    r"unable to (access|review|retrieve)|error preventing|"
    r"no accessible|content is (blocked|unavailable)",
    re.IGNORECASE,
)


def _strip_junk_issues(brands: list[dict]) -> list[dict]:
    """Drop fetch-error hallucinations from each brand's issues list."""
    for b in brands or []:
        if not b.get("issues"):
            continue
        b["issues"] = [s for s in b["issues"] if s and not _JUNK_ISSUE_RE.search(str(s))]
    return brands


# content_kind of a row whose brands were extracted from a Perplexity
# summary because the page itself could not be read. Older rows used
# "perplexity" for the same thing.
SUMMARY_KIND = "summary"
SUMMARY_KINDS = frozenset({SUMMARY_KIND, "perplexity"})


def is_summary_derived(result) -> bool:
    return (result.content_kind or "") in SUMMARY_KINDS


# Lanes the UI draws a load sign for. Ordered as they run so the graph
# fills left to right. "analysis" covers the per-URL read+extract loop,
# which is the slow one; the discovery lanes finish in seconds.
STAGES = ("web", "serp_features", "community", "engines", "analysis")

PENDING = "pending"
RUNNING = "running"
COMPLETE = "complete"
SKIPPED = "skipped"
FAILED = "failed"


def _init_stages(scan: SourceScan) -> None:
    """Seed every lane as pending so the UI can render the full strip
    (including lanes that have not started) from the first poll."""
    scan.stages = {name: {"status": PENDING, "count": 0, "detail": ""} for name in STAGES}
    scan.save(update_fields=["stages", "updated_at"])


def _set_stage(scan: SourceScan, name: str, status: str, *, count=None, detail=None) -> None:
    """Update one lane and persist immediately.

    Saved on its own so a poll landing mid-scan sees the lane flip in
    real time. Never raises: a progress-reporting failure must not take
    down the scan it is reporting on.
    """
    try:
        stages = dict(scan.stages or {})
        lane = dict(stages.get(name) or {"status": PENDING, "count": 0, "detail": ""})
        lane["status"] = status
        if count is not None:
            lane["count"] = count
        if detail is not None:
            lane["detail"] = str(detail)[:200]
        stages[name] = lane
        scan.stages = stages
        scan.save(update_fields=["stages", "updated_at"])
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("stage update %s=%s failed on scan %s: %s", name, status, scan.pk, exc)


def _finalize_stages(scan: SourceScan, *, reason: str = "") -> None:
    """Force every non-terminal lane to a terminal state.

    Without this a lane that raised (or that an early return skipped)
    would spin forever in the UI, because the frontend keys its load
    sign off the lane status rather than the scan status.
    """
    try:
        stages = dict(scan.stages or {})
        changed = False
        for name in STAGES:
            lane = dict(stages.get(name) or {"status": PENDING, "count": 0, "detail": ""})
            if lane.get("status") in (PENDING, RUNNING):
                lane["status"] = SKIPPED if lane.get("status") == PENDING else FAILED
                if reason and not lane.get("detail"):
                    lane["detail"] = reason[:200]
                stages[name] = lane
                changed = True
        if changed:
            scan.stages = stages
            scan.save(update_fields=["stages", "updated_at"])
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("stage finalize failed on scan %s: %s", scan.pk, exc)


def _discover(scan: SourceScan, website) -> list[dict]:
    """Run every discovery lane and return one ranked, deduped row list.

    Three lanes feed this:
      - Perplexity search (AI-era index)
      - Google organic via SerpAPI
      - Community threads (Reddit search + Google's discussions block)

    Web rows are merged across the two indexes so agreement between them
    counts as a relevance signal. Community rows keep their own quota so a
    crowded web lane can never squeeze them out.

    Each lane is independently best-effort: the scan only fails if every
    lane came back empty. Lane status is reported as we go so the UI can
    show which parts are still working.
    """
    query = scan.query

    # -- web: Perplexity -------------------------------------------------------
    _set_stage(scan, "web", RUNNING)
    try:
        perplexity_rows = web_search.search_web(
            query, max_results=MAX_WEB_RESULTS, user_id=website.user_id,
        ) or []
    except Exception as exc:  # defensive: the client normally returns []
        logger.warning("discovery: perplexity failed on scan %s: %s", scan.pk, exc)
        perplexity_rows = []

    # -- web + features: Google via SerpAPI ------------------------------------
    _set_stage(scan, "serp_features", RUNNING)
    try:
        google = serp_google.search(query, max_results=MAX_WEB_RESULTS)
    except Exception as exc:  # defensive: serp_google swallows its own errors
        logger.warning("discovery: serpapi failed on scan %s: %s", scan.pk, exc)
        google = None

    if not google or not google.get("configured"):
        scan.serp_features = {}
        scan.save(update_fields=["serp_features", "updated_at"])
        _set_stage(scan, "serp_features", SKIPPED, detail="serpapi_not_configured")
        google = google or {}
    elif google.get("error"):
        _set_stage(scan, "serp_features", FAILED, detail=google["error"])
    else:
        features = {
            "questions": google.get("questions") or [],
            "related_searches": google.get("related_searches") or [],
            "ai_overview": google.get("ai_overview") or {},
            "knowledge_graph": google.get("knowledge_graph") or {},
        }
        scan.serp_features = features
        scan.save(update_fields=["serp_features", "updated_at"])
        _set_stage(
            scan, "serp_features", COMPLETE,
            count=len(features["questions"]) + len(features["related_searches"]),
        )

    web_rows = serp_google.merge_discovery(
        perplexity_rows, google.get("organic") or [], limit=MAX_WEB_RESULTS,
    )
    if web_rows:
        _set_stage(scan, "web", COMPLETE, count=len(web_rows))
    else:
        _set_stage(scan, "web", FAILED, detail="no results")

    # -- community: Reddit search + Google's discussions block -----------------
    _set_stage(scan, "community", RUNNING)
    try:
        reddit_rows = community.discover_reddit(query, limit=MAX_COMMUNITY_RESULTS)
    except Exception as exc:  # defensive: the lane swallows its own errors
        logger.warning("discovery: reddit failed on scan %s: %s", scan.pk, exc)
        reddit_rows = []
    discussion_rows = community.from_serp_discussions(
        google.get("discussions") or [], limit=MAX_COMMUNITY_RESULTS,
    )
    community_rows = serp_google.merge_discovery(
        reddit_rows, discussion_rows, limit=MAX_COMMUNITY_RESULTS,
    )
    # A thread already found by the web lane is not a second source.
    web_keys = {_dedupe_key(r["url"]) for r in web_rows}
    community_rows = [r for r in community_rows if _dedupe_key(r["url"]) not in web_keys]
    for index, row in enumerate(community_rows, start=1):
        row["rank"] = index
    if not community_rows and not community.is_enabled():
        # "Complete, 0 found" would claim we looked and the conversation is
        # empty. We did not look.
        _set_stage(scan, "community", SKIPPED, detail="community lane disabled")
    else:
        _set_stage(scan, "community", COMPLETE, count=len(community_rows))

    # -- stitch ----------------------------------------------------------------
    # Rank is the row's global position, which downstream code needs dense
    # and unique. Lane rank is kept separately because that is what should
    # drive share-of-voice weighting: the top community thread carries as
    # much signal as the top web result, and would be near-worthless at a
    # global rank of 11.
    ordered = []
    for row in web_rows:
        row["platform_meta"] = {
            **(row.get("platform_meta") or {}),
            "lane": "web",
            "lane_rank": row["rank"],
        }
        ordered.append(row)
    for row in community_rows:
        row["platform_meta"] = {
            **(row.get("platform_meta") or {}),
            "lane": "community",
            "lane_rank": row["rank"],
        }
        ordered.append(row)

    for index, row in enumerate(ordered[:MAX_RESULTS_PER_SCAN], start=1):
        row["rank"] = index
    return ordered[:MAX_RESULTS_PER_SCAN]


def _dedupe_key(url: str) -> str:
    from apps.citations.services.url_normalizer import normalize_url
    return normalize_url(url)[0]


def run_scan(scan: SourceScan) -> SourceScan:
    """Execute all stages. Always leaves the scan in COMPLETE or FAILED."""
    scan.status = SourceScanStatus.RUNNING
    scan.save(update_fields=["status", "updated_at"])
    _init_stages(scan)

    website = scan.website
    target_brand = website.name or ""

    if scan.seed_urls:
        # Seeded scan: the caller already knows exactly which URLs to
        # analyze (e.g. the URLs an AI answer cited for a prompt), so
        # the SERP search and its relevance gate are skipped entirely.
        _set_stage(scan, "web", RUNNING)
        rows = []
        for rank, url in enumerate(scan.seed_urls[:MAX_RESULTS_PER_SCAN], start=1):
            host = urlparse(url).netloc or url
            rows.append(SourceScanResult(
                scan=scan,
                rank=rank,
                url=str(url)[:2000],
                domain=host[:300],
                source_class=_rule_classify(extract_apex_domain(host), host),
                relevant=True,
                relevance_note="seeded from prompt citations",
                discovery="seed",
                platform_meta={"lane": "web", "lane_rank": rank},
            ))
        _set_stage(scan, "web", COMPLETE, count=len(rows), detail="seeded from prompt citations")
        # A seeded scan is explicitly told which URLs to read, so the
        # discovery lanes have nothing to do.
        _set_stage(scan, "serp_features", SKIPPED, detail="seeded scan")
        _set_stage(scan, "community", SKIPPED, detail="seeded scan")
    else:
        discovered = _discover(scan, website)
        if not discovered:
            return _fail(
                scan,
                "no search results (search keys missing, quota exhausted, or empty SERP)",
            )

        # Relevance gate: one batched judgement on titles/urls so off-topic
        # noise (designer "bags" on a "bagels" query) is dropped before we
        # pay for content fetches and per-result analysis.
        verdicts = source_sentiment.assess_serp_relevance(
            scan.query, discovered, website=website, user=scan.created_by,
        )

        rows = []
        for item in discovered:
            verdict = verdicts.get(item["rank"], {"relevant": True, "reason": ""})
            domain = item.get("domain") or ""
            rows.append(SourceScanResult(
                scan=scan,
                rank=item["rank"],
                url=item["url"][:2000],
                domain=domain[:300],
                source_class=_rule_classify(extract_apex_domain(domain), domain),
                serp_title=(item.get("title") or "")[:500],
                serp_snippet=item.get("snippet") or "",
                published_at=_parse_date(item.get("date")),
                last_updated_at=_parse_date(item.get("last_updated")),
                relevant=verdict["relevant"],
                relevance_note=verdict["reason"][:200],
                discovery=",".join(item.get("discovered_by") or [])[:64],
                platform_meta=item.get("platform_meta") or {},
            ))
    SourceScanResult.objects.bulk_create(rows)
    scan.results_count = len(rows)
    scan.save(update_fields=["results_count", "updated_at"])

    # Engines run before the analysis loop: they are fast, and they give the
    # user a populated graph to look at while the per-URL reads grind.
    _run_engine_lane(scan, website, target_brand)

    _set_stage(scan, "analysis", RUNNING, count=0)
    analyzed = 0
    for result in scan.results.all():
        if not result.relevant:
            result.analysis_error = "off_topic"
            result.save()
            continue

        content = content_reader.read_url(result.url)
        result.fetch_status = content["status"]
        result.content_kind = content["kind"]
        result.word_count = content["word_count"]
        if content["status"] != "ok" or not content["text"].strip():
            # Direct fetch failed (403, blocked, empty body). Perplexity
            # can still summarize sources our fetcher can't reach (Reddit
            # anti-bot walls, paywalled sites), and brands are extracted
            # from that summary instead. fetch_status keeps the real fetch
            # outcome and content_kind marks the row as summary-derived,
            # so the row is never presented as a page we read.
            fallback = web_search.summarize_url_with_perplexity(
                result.url,
                query=scan.query,
                user_id=website.user_id,
            )
            if fallback and fallback.get("text", "").strip():
                content = {
                    "status": content["status"],
                    "kind": SUMMARY_KIND,
                    "text": fallback["text"],
                    "word_count": fallback.get("word_count") or len(fallback["text"].split()),
                    "detail": "perplexity summary",
                }
                result.content_kind = SUMMARY_KIND
                result.word_count = content["word_count"]
            else:
                result.analysis_error = content["detail"] or "no content"
                result.save()
                continue

        analysis = source_sentiment.analyze_content(
            content["text"],
            query=scan.query,
            target_brand=target_brand,
            website=website,
            user=scan.created_by,
        )
        if analysis.get("error"):
            result.analysis_error = analysis["error"]
        elif not analysis.get("relevant_to_query", True):
            # Title looked on-topic but the content was not.
            result.relevant = False
            result.relevance_note = "content off-topic"
            result.analysis_error = "off_topic"
            result.brands = []
        else:
            result.brands = _strip_junk_issues(analysis["brands"])
            analyzed += 1
        result.save()
        # Report after every row: this loop is the slow part of a scan
        # (one fetch + one LLM call per URL), so a live count is the
        # difference between "working" and "hung" from the UI's side.
        _set_stage(scan, "analysis", RUNNING, count=analyzed)

    _set_stage(scan, "analysis", COMPLETE, count=analyzed)
    scan.analyzed_count = analyzed
    scan.brands = _aggregate(scan, target_brand)
    scan.own_brand_present = any(
        b.get("is_own_brand") for b in scan.brands
    )
    scan.status = SourceScanStatus.COMPLETE
    scan.completed_at = timezone.now()
    scan.save()
    _finalize_stages(scan)
    logger.info(
        "SourceScan %s complete: %d results, %d analyzed, %d brands",
        scan.pk, scan.results_count, analyzed, len(scan.brands),
    )
    return scan


def _run_engine_lane(scan: SourceScan, website, target_brand: str) -> None:
    """Ask every AI engine the scan query and persist what each recommends.

    Best-effort in full: a failure here leaves the lane marked failed and
    the rest of the scan untouched. A seeded scan skips it -- those analyze
    a fixed URL list for a prompt that was already run against the engines
    elsewhere, so probing again would double-charge for the same answer.
    """
    if scan.seed_urls:
        _set_stage(scan, "engines", SKIPPED, detail="seeded scan")
        return
    if not engine_probe.is_enabled():
        _set_stage(scan, "engines", SKIPPED, detail="engine lane disabled")
        return

    _set_stage(scan, "engines", RUNNING, count=0)
    try:
        engine_rows = engine_probe.probe_all(
            query=scan.query,
            target_brand=target_brand,
            website=website,
            user=scan.created_by,
            on_progress=lambda n: _set_stage(scan, "engines", RUNNING, count=n),
        )

        # Google's AI Overview rides along free with the SERP call already
        # made in discovery, so it costs nothing extra to include.
        overview = (scan.serp_features or {}).get("ai_overview") or {}
        if overview:
            extra = engine_probe.row_from_ai_overview(
                overview, query=scan.query, target_brand=target_brand,
                website=website, user=scan.created_by,
            )
            if extra:
                engine_rows.append(extra)

        source_rows = list(scan.results.all())
        linked = engine_probe.link_citations_to_rows(engine_rows, source_rows)

        SourceScanEngineAnswer.objects.filter(scan=scan).delete()
        SourceScanEngineAnswer.objects.bulk_create([
            SourceScanEngineAnswer(
                scan=scan,
                provider=row["provider"][:32],
                model=(row.get("model") or "")[:80],
                status=row["status"],
                answer_text=row.get("answer_text") or "",
                brands=row.get("brands") or [],
                citations=row.get("citations") or [],
                cited_ranks=linked.get(row["provider"], []),
                error=(row.get("error") or "")[:300],
            )
            for row in engine_rows
        ])
        answered = sum(1 for r in engine_rows if r["status"] == engine_probe.STATUS_OK)
        _set_stage(scan, "engines", COMPLETE, count=answered)
    except Exception as exc:
        logger.warning("engine lane failed on scan %s: %s", scan.pk, exc)
        _set_stage(scan, "engines", FAILED, detail=str(exc))


def _fail(scan: SourceScan, message: str) -> SourceScan:
    scan.status = SourceScanStatus.FAILED
    scan.error = message[:1000]
    scan.completed_at = timezone.now()
    scan.save(update_fields=["status", "error", "completed_at", "updated_at"])
    _finalize_stages(scan, reason=message)
    logger.warning("SourceScan %s failed: %s", scan.pk, message)
    return scan


def _parse_date(value) -> date | None:
    """Parse a search-engine date string (ISO YYYY-MM-DD prefix) or None."""
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _matches_target(name: str, target: str) -> bool:
    """Fuzzy own-brand match: exact or substring either way, lowercased."""
    key = (name or "").strip().lower()
    target_lower = (target or "").strip().lower()
    if not key or not target_lower:
        return False
    return key == target_lower or target_lower in key or key in target_lower


def _weighting_rank(result) -> int:
    """The rank share-of-voice should weight by.

    ``rank`` is the row's global position across all lanes, which keeps it
    dense and unique for the DB. But a scan's top community thread lands at
    global rank 11 or so, and weighting that at 1/11 would bury the exact
    human signal this lane was added to surface. Weight by position within
    the lane instead, so each lane's #1 counts as a #1.
    """
    lane_rank = (result.platform_meta or {}).get("lane_rank")
    try:
        lane_rank = int(lane_rank)
    except (TypeError, ValueError):
        lane_rank = 0
    return lane_rank if lane_rank > 0 else max(1, result.rank)


def _aggregate(scan: SourceScan, target_brand: str) -> list[dict]:
    """Roll per-result brand extractions into a share-of-voice list.

    Rank-weighted: a mention in the #1 result counts more than one in
    the #10 result. Sentiment is a weight-averaged blend.
    """
    buckets: dict[str, dict] = defaultdict(
        lambda: {"mentions": 0, "weighted_score": 0.0, "sentiment_num": 0.0,
                 "sentiment_den": 0.0, "results_present_in": 0, "top_quote": "",
                 "top_quote_weight": -1.0, "issues": [], "engines": []}
    )

    def _absorb(brand: dict, rank_weight: float, *, engine: str = "") -> None:
        key = brand["name"].strip().lower()
        bucket = buckets[key]
        bucket.setdefault("name", brand["name"])
        contribution = rank_weight * (brand.get("weight") or 0.0)
        bucket["mentions"] += brand.get("mentions") or 0
        bucket["weighted_score"] += contribution
        bucket["sentiment_num"] += (brand.get("sentiment") or 0.0) * max(contribution, 0.01)
        bucket["sentiment_den"] += max(contribution, 0.01)
        bucket["results_present_in"] += 1
        if engine and engine not in bucket["engines"]:
            bucket["engines"].append(engine)
        quotes = brand.get("quotes") or []
        if quotes and contribution > bucket["top_quote_weight"]:
            bucket["top_quote"] = quotes[0]
            bucket["top_quote_weight"] = contribution
        for issue in brand.get("issues") or []:
            bucket["issues"].append((contribution, str(issue)))

    for result in scan.results.filter(relevant=True):
        rank_weight = 1.0 / _weighting_rank(result)  # 1, .5, .33, ...
        for brand in result.brands or []:
            _absorb(brand, rank_weight)

    # An engine naming a brand is the strongest visibility signal in the
    # scan -- it is the answer the customer sees, not a page they might
    # click -- so engine mentions weight the same as a lane's #1 result.
    for answer in scan.engine_answers.all():
        if answer.status != "ok":
            continue
        for brand in answer.brands or []:
            _absorb(brand, ENGINE_RANK_WEIGHT, engine=answer.provider)

    rollup = []
    for key, bucket in buckets.items():
        # Up to 5 issues, highest-contribution sources first, deduped
        # case-insensitively.
        seen: set[str] = set()
        issues = []
        for _, issue in sorted(bucket["issues"], key=lambda p: p[0], reverse=True):
            fold = issue.strip().lower()
            if fold and fold not in seen:
                seen.add(fold)
                issues.append(issue)
            if len(issues) >= 5:
                break
        rollup.append({
            "name": bucket["name"],
            "mentions": bucket["mentions"],
            "engines": bucket["engines"],
            "engine_mentions": len(bucket["engines"]),
            "weighted_score": round(bucket["weighted_score"], 4),
            "sentiment": round(bucket["sentiment_num"] / bucket["sentiment_den"], 3)
            if bucket["sentiment_den"] else 0.0,
            "results_present_in": bucket["results_present_in"],
            "top_quote": bucket["top_quote"],
            "issues": issues,
            "is_own_brand": _matches_target(key, target_brand),
        })
    rollup.sort(key=lambda b: b["weighted_score"], reverse=True)
    return rollup[:25]


# Source classes where a brand can realistically join the conversation:
# a comment, reply, or answer box exists on the page. A blog post or
# news article with no comment section is not an opportunity even if
# competitors dominate it — there's nowhere to chip in.
_ENGAGEABLE_CLASSES = {"reddit", "forum", "quora", "youtube"}
_ENGAGEABLE_CLASS_BOOST = {c: 1.5 for c in _ENGAGEABLE_CLASSES}


MAX_OPPORTUNITIES = 12


def _age_days(published_at) -> int | None:
    if not published_at:
        return None
    return max(0, (date.today() - published_at).days)


def derive_opportunities(scan: SourceScan) -> list[dict]:
    """Where the user can go say something about their own brand.

    Two kinds:

    - ``thread``: a live discussion where competitors are named and the
      user's brand is not, on a page that actually has a reply box.
    - ``question``: a People Also Ask question from the SERP. Nobody owns
      the answer, so publishing one is an opening of a different shape.

    Computed at read time, so it works retroactively on scans that predate
    any of this and needs no schema of its own.
    """
    target = scan.website.name or ""
    opportunities = []
    for result in scan.results.all():
        # A summary can leave the user's brand out even when the page
        # names it, so "competitors named, you are not" needs the page.
        if not result.relevant or result.fetch_status != "ok" or is_summary_derived(result):
            continue
        # Hard gate: only surface sources with a comment/reply surface.
        # News, blogs, guides, review aggregators without user contribution
        # are excluded even if competitors dominate them.
        if result.source_class not in _ENGAGEABLE_CLASSES:
            continue
        brands = result.brands or []
        if not brands or any(_matches_target(b.get("name", ""), target) for b in brands):
            continue

        meta = result.platform_meta or {}
        num_comments = int(meta.get("num_comments") or 0)
        age = _age_days(result.published_at)

        weight_sum = sum(b.get("weight") or 0.0 for b in brands)
        boost = _ENGAGEABLE_CLASS_BOOST.get(result.source_class, 1.0)
        # An unanswered thread is worth less than a busy one, and a busy
        # one going stale is worth less than a busy one from last week.
        # Both are gentle multipliers so they re-order rather than dominate.
        activity = 1.0 + min(math.log10(num_comments + 1) / 3.0, 0.6)
        freshness = 1.0 if age is None else max(0.6, 1.0 - math.log10(age + 1) / 10.0)
        score = (1.0 / _weighting_rank(result)) * weight_sum * boost * activity * freshness

        competitors = [b.get("name", "") for b in brands[:5]]
        issues = []
        for b in brands:
            issues.extend((b.get("issues") or [])[:2])
        reason = (
            f"Active thread ranking #{result.rank} for this query mentions "
            f"{', '.join(competitors[:3])} but not {target or 'your brand'}."
        )
        opportunities.append({
            "kind": "thread",
            "rank": result.rank,
            "url": result.url,
            "domain": result.domain,
            "source_class": result.source_class,
            "serp_title": result.serp_title,
            "published_at": result.published_at.isoformat() if result.published_at else None,
            "age_days": age,
            "subreddit": meta.get("subreddit") or "",
            "num_comments": num_comments,
            "score_upvotes": int(meta.get("score") or 0),
            "competitors": competitors,
            "issues": issues[:5],
            "reason": reason,
            "score": round(score, 4),
        })
    opportunities.sort(key=lambda o: o["score"], reverse=True)
    opportunities = opportunities[:MAX_OPPORTUNITIES]

    # People Also Ask: questions real users type that the user could answer
    # on their own site. Appended after threads because replying to a live
    # thread is a smaller, faster action than publishing a page.
    questions = (scan.serp_features or {}).get("questions") or []
    for index, question in enumerate(questions[:5]):
        text = (question.get("question") or "").strip()
        if not text:
            continue
        opportunities.append({
            "kind": "question",
            "rank": None,
            "url": question.get("url") or "",
            "domain": question.get("domain") or "",
            "source_class": "other",
            "serp_title": text,
            "published_at": None,
            "age_days": None,
            "subreddit": "",
            "num_comments": 0,
            "score_upvotes": 0,
            "competitors": [],
            "issues": [],
            "reason": (
                "People ask this on Google for your query. Answering it on "
                "your own site is a way in that does not depend on a thread."
            ),
            "score": round(0.5 / (index + 1), 4),
        })
    return opportunities
