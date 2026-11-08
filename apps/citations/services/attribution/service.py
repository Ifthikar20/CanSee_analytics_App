"""Source attribution for one AI answer (docs/attribution_contract.md).

Pipeline, run on demand the first time a response is opened:

1. Candidate sources: the pages the engine cited (its reported passages
   first), then up to a few of the brand's own pages whose text overlaps
   the answer.
2. Read each candidate once into a PageSnapshot (reader blocks), reusing a
   recent snapshot.
3. Ask the analysis model which passage of which page each statement of
   the answer comes from, and for an edit on the brand's own passages.
4. Keep only findings whose answer quote is in the answer and whose page
   quote is in the block the model named; mark a finding ``confirmed``
   when the engine itself cited that page for that part of the answer.
   Engine-cited passages the model did not cover are added as confirmed
   findings on their own.
5. Store the findings and a "where you stand" summary.
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.citations.models import (
    AttributionStatus,
    Citation,
    PageSnapshot,
    ResponseAttribution,
)
from apps.citations.services.attribution import prompts
from apps.citations.services.attribution.source_types import (
    OWNER_YOU,
    classify_source,
    site_apex,
)
from apps.citations.services.attribution.text_match import (
    block_texts,
    locate,
    locate_in_blocks,
)
from apps.citations.services.url_normalizer import extract_apex_domain, normalize_url

logger = logging.getLogger("apps")

VERSION = "1"
MAX_THIRD_PARTY = 4
MAX_OWN = 3
SNAPSHOT_TTL = timedelta(days=7)
# A pending run older than this is treated as lost and may be restarted.
PENDING_STALE = timedelta(minutes=5)
FETCH_WORKERS = 3
ANALYSIS_TIMEOUT = 90.0
ANALYSIS_MAX_TOKENS = 4000
MIN_OWN_OVERLAP = 3

_WORD = re.compile(r"[a-z0-9][a-z0-9'-]{3,}")
_STOP = frozenset({
    "this", "that", "with", "from", "your", "they", "their", "have", "will", "which",
    "about", "there", "these", "those", "into", "more", "most", "best", "also", "such",
    "when", "what", "where", "than", "then", "them", "been", "were", "some", "each",
    "other", "offers", "offer", "provides", "provide", "include", "includes",
})

ENGINE_REASON = "The engine cited this page for this part of its answer."


# ── Public API ────────────────────────────────────────────────────────────


def _result_for(website, result_id):
    from apps.llm_ranking.models import LLMRankingResult

    try:
        return (
            LLMRankingResult.objects.select_related("audit")
            .get(public_id=result_id, audit__website=website)
        )
    except (LLMRankingResult.DoesNotExist, ValueError):
        return None


def payload(result, attribution: ResponseAttribution | None) -> dict:
    """The contract's attribution payload for ``result``."""
    if attribution is None:
        return {"result_id": str(result.public_id), "status": "none", "error": None,
                "updated_at": None, "summary": {}, "findings": []}
    ready = attribution.status == AttributionStatus.READY
    return {
        "result_id": str(result.public_id),
        "status": attribution.status,
        "error": attribution.error or None if attribution.status == AttributionStatus.FAILED else None,
        "updated_at": attribution.updated_at.isoformat() if attribution.updated_at else None,
        "summary": attribution.summary if ready else {},
        "findings": attribution.findings if ready else [],
    }


def get_payload(website, result_id) -> dict | None:
    """Stored attribution for one response of ``website`` (never starts
    work), or None when the response is not this website's."""
    result = _result_for(website, result_id)
    if result is None:
        return None
    attribution = ResponseAttribution.objects.filter(result=result).first()
    return payload(result, attribution)


def start(website, result_id, *, user, refresh: bool = False) -> dict | None:
    """Start (or with ``refresh`` re-run) the analysis and return the
    payload. A ready attribution is returned as is unless ``refresh``; a
    run already in progress is not started twice."""
    from apps.citations.tasks import compute_response_attribution

    result = _result_for(website, result_id)
    if result is None:
        return None
    now = timezone.now()
    with transaction.atomic():
        attribution, created = ResponseAttribution.objects.select_for_update().get_or_create(
            result=result, defaults={"website": website, "status": AttributionStatus.PENDING,
                                     "started_at": now},
        )
        if not created:
            if attribution.status == AttributionStatus.READY and not refresh:
                return payload(result, attribution)
            running = (attribution.status == AttributionStatus.PENDING and attribution.started_at
                       and now - attribution.started_at < PENDING_STALE)
            if running:
                return payload(result, attribution)
            attribution.status = AttributionStatus.PENDING
            attribution.error = ""
            attribution.started_at = now
            attribution.finished_at = None
            attribution.save(update_fields=["status", "error", "started_at", "finished_at",
                                            "updated_at"])
    # The row is committed (the atomic block above has closed), so the
    # worker sees it.
    user_id = getattr(user, "pk", None)
    compute_response_attribution.delay(str(attribution.id), str(user_id) if user_id else None)
    # With eager tasks (dev, tests) the run has finished by now.
    attribution.refresh_from_db()
    return payload(result, attribution)


def snapshot_payload(website, url: str) -> dict | None:
    """The stored snapshot of ``url``, only when one of this website's
    attributions points at it (so this is never an open fetcher)."""
    url = (url or "").strip()
    if not url:
        return None
    referenced = ResponseAttribution.objects.filter(
        website=website, source_urls__contains=[url],
    ).exists()
    if not referenced:
        return None
    snap = PageSnapshot.objects.filter(website=website, url=url).first()
    if snap is None:
        return None
    return {
        "url": snap.url,
        "final_url": snap.final_url or snap.url,
        "title": snap.title,
        "captured_at": snap.captured_at.isoformat() if snap.captured_at else None,
        "error": snap.error or None,
        "blocks": snap.blocks or [],
    }


# ── The run ───────────────────────────────────────────────────────────────


def compute(attribution_id: str, user_id: str | None = None) -> None:
    """Run the pipeline for one attribution; never raises."""
    attribution = (
        ResponseAttribution.objects.select_related("result", "result__audit", "website")
        .filter(id=attribution_id).first()
    )
    if attribution is None:
        return
    try:
        findings, summary, urls, error = _analyse(attribution, user_id)
    except Exception:
        logger.exception("attribution %s failed", attribution_id)
        findings, summary, urls, error = [], {}, [], "analysis_failed"
    if error and not findings:
        attribution.status = AttributionStatus.FAILED
        attribution.error = error[:40]
    else:
        attribution.status = AttributionStatus.READY
        attribution.error = ""
    attribution.findings = findings
    attribution.summary = summary
    attribution.source_urls = urls
    attribution.version = VERSION
    attribution.finished_at = timezone.now()
    attribution.save(update_fields=["status", "error", "findings", "summary", "source_urls",
                                    "version", "finished_at", "updated_at"])


def _analyse(attribution, user_id):
    result = attribution.result
    website = attribution.website
    answer = result.response_text or ""
    if not answer.strip() or not result.query_succeeded:
        return [], _summary(result, website, []), [], "no_answer"

    comps = _competitor_apexes(website, result)
    engine = [p for p in (result.citation_passages or []) if isinstance(p, dict)]
    sources = _candidate_sources(website, result, answer, engine, comps)
    _read_pages(website, sources)

    by_id = {s["id"]: s for s in sources}
    engine_hints = []
    for p in engine:
        src = _source_by_url(sources, p.get("url"))
        if src is not None:
            engine_hints.append({"source_id": src["id"],
                                 "answer_text": answer[p["answer_start"]:p["answer_end"]]})

    candidates: list[dict] = []
    error = ""
    readable = [s for s in sources if s["pieces"]]
    if readable:
        user = _user(user_id) or getattr(website, "user", None)
        from apps.agent.llm.worker import ask_haiku_standalone

        asked = ask_haiku_standalone(
            user=user, website=website, system=prompts.SYSTEM,
            content=prompts.content(
                brand={"name": website.name or "", "website": getattr(website, "url", "") or "",
                       "competitors": [c.get("name") for c in (website.competitors or [])
                                       if isinstance(c, dict) and c.get("name")][:10]},
                question=result.prompt or "", answer=answer, engine_citations=engine_hints,
                sources=readable,
            ),
            schema=prompts.SCHEMA, output_model=prompts.Attribution,
            timeout=ANALYSIS_TIMEOUT, audit_id=str(result.public_id), role="attribution",
            step=f"{attribution.id}:{int(attribution.started_at.timestamp()) if attribution.started_at else 0}",
            module="citations", max_tokens=ANALYSIS_MAX_TOKENS,
        )
        if asked.ok and asked.data is not None:
            candidates.extend(_validated(asked.data.findings, by_id, answer, engine))
        else:
            error = asked.code or "analysis_failed"
    elif sources:
        error = "fetch_failed"

    candidates.extend(_engine_only(engine, sources, answer, candidates))
    findings = _finalize(candidates)
    urls = sorted({s["url"] for s in sources if s.get("snapshot_saved")})
    return findings, _summary(result, website, findings), urls, error


# ── Sources ───────────────────────────────────────────────────────────────


def _norm(url) -> str:
    if not isinstance(url, str) or not url:
        return ""
    try:
        return normalize_url(url)[0] or url
    except Exception:
        return url


def _source_by_url(sources, url):
    key = _norm(url)
    for s in sources:
        if s["key"] == key:
            return s
    return None


def _competitor_apexes(website, result) -> set[str]:
    from apps.citations.services.domain_classifier import collect_competitor_apexes

    out = set()
    try:
        out |= collect_competitor_apexes(website)
    except Exception:
        pass
    for c in list(result.competitors_mentioned or []) + list(website.competitors or []):
        if isinstance(c, dict):
            for key in ("domain", "url", "website"):
                value = c.get(key)
                if isinstance(value, str) and value:
                    host = value.split("://", 1)[-1].split("/", 1)[0].lower()
                    apex = extract_apex_domain(host[4:] if host.startswith("www.") else host)
                    if apex:
                        out.add(apex)
    own = site_apex(website)
    out.discard(own)
    return out


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOP}


def _own_candidates(website, answer: str, exclude: set[str]) -> list[str]:
    """Up to MAX_OWN own-site URLs whose known text overlaps the answer."""
    own = site_apex(website)
    if not own:
        return []
    texts: dict[str, list[str]] = {}

    def add(url, text):
        key = _norm(url)
        if not key or key in exclude:
            return
        host = (normalize_url(url)[2] if url else "") or ""
        if host != own:
            return
        texts.setdefault(url, []).append(text or "")

    try:
        from apps.insights.models import EvidencePage

        for url, text in EvidencePage.objects.filter(website=website).values_list("url", "text")[:50]:
            add(url, text)
    except Exception:
        pass
    try:
        from apps.rag.models import KnowledgeChunk

        rows = (KnowledgeChunk.objects.filter(website=website)
                .values_list("source__url", "text")[:400])
        for url, text in rows:
            add(url, text)
    except Exception:
        pass
    for snap in PageSnapshot.objects.filter(website=website).only("url", "blocks")[:50]:
        add(snap.url, " ".join(t for _, _, t in block_texts(snap.blocks)))

    answer_words = _words(answer)
    scored = []
    for url, chunks in texts.items():
        overlap = len(answer_words & _words(" ".join(chunks)))
        if overlap >= MIN_OWN_OVERLAP:
            scored.append((overlap, url))
    scored.sort(key=lambda x: (-x[0], x[1]))
    picked = [u for _, u in scored[:MAX_OWN]]
    if not picked and getattr(website, "url", ""):
        home = website.url if "://" in website.url else f"https://{website.url}"
        if _norm(home) not in exclude:
            picked = [home]
    return picked


def _candidate_sources(website, result, answer, engine, comps) -> list[dict]:
    """Ordered candidate pages: engine-cited, then cited, then own pages."""
    urls: list[str] = []
    seen: set[str] = set()

    def push(url):
        key = _norm(url)
        if key and key not in seen and url.startswith(("http://", "https://")):
            seen.add(key)
            urls.append(url)

    for p in engine:
        push(p.get("url"))
    for url in (Citation.objects.filter(result=result).order_by("position", "id")
                .values_list("url", flat=True)[:20]):
        push(url)

    own_cited, others = [], []
    for url in urls:
        kind = classify_source(url, website=website, competitor_apexes=comps)
        (own_cited if kind["owner"] == OWNER_YOU else others).append((url, kind))
    own_cited = own_cited[:MAX_OWN]
    others = others[:MAX_THIRD_PARTY]
    room = MAX_OWN - len(own_cited)
    extra = []
    if room > 0:
        for url in _own_candidates(website, answer, seen)[:room]:
            extra.append((url, classify_source(url, website=website, competitor_apexes=comps)))

    sources = []
    for i, (url, kind) in enumerate(own_cited + others + extra, start=1):
        sources.append({"id": f"S{i}", "url": url, "key": _norm(url), "title": "",
                        "final_url": url, "blocks": [], "pieces": [], **kind})
    return sources


def _read_pages(website, sources) -> None:
    """Fill each source's blocks from a fresh snapshot or a new read."""
    from apps.agent.services.browse import fetch_page

    fresh_after = timezone.now() - SNAPSHOT_TTL
    to_fetch = []
    for src in sources:
        snap = PageSnapshot.objects.filter(website=website, url=src["url"]).first()
        if snap and not snap.error and snap.blocks and snap.captured_at and snap.captured_at >= fresh_after:
            _apply(src, snap)
        else:
            to_fetch.append(src)
    if not to_fetch:
        return

    def read(src):
        try:
            return src, fetch_page(src["url"])
        except Exception:
            logger.warning("attribution: reading %s failed", src["url"], exc_info=True)
            return src, None

    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        results = list(pool.map(read, to_fetch))
    for src, page in results:
        if page is None:
            continue
        error = (page.get("error") or {}).get("code") or ""
        reader = page.get("reader") or {}
        snap, _ = PageSnapshot.objects.update_or_create(
            website=website, url=src["url"],
            defaults={
                "final_url": (page.get("final_url") or src["url"])[:2000],
                "title": (page.get("title") or "")[:300],
                "blocks": reader.get("blocks") or [],
                "status_code": page.get("status_code"),
                "error": error[:40],
                "captured_at": timezone.now(),
            },
        )
        _apply(src, snap)


def _apply(src: dict, snap: PageSnapshot) -> None:
    src["blocks"] = snap.blocks or []
    src["pieces"] = [p for p in block_texts(src["blocks"])]
    src["title"] = snap.title or src.get("title") or ""
    src["final_url"] = snap.final_url or src["url"]
    src["snapshot_saved"] = True


# ── Findings ──────────────────────────────────────────────────────────────


def _source_payload(src: dict) -> dict:
    return {
        "url": src["url"],
        "final_url": src.get("final_url") or src["url"],
        "title": src.get("title") or src.get("domain") or src["url"],
        "domain": src.get("domain") or "",
        "owner": src["owner"],
        "type": src["type"],
        "type_label": src["type_label"],
    }


def _engine_overlap(engine, src, start, end) -> bool:
    for p in engine:
        if _norm(p.get("url")) == src["key"] and p["answer_start"] < end and start < p["answer_end"]:
            return True
    return False


def _block_hint(block_id: str) -> int | None:
    m = re.fullmatch(r"B?(\d{1,4})", (block_id or "").strip(), flags=re.I)
    return int(m.group(1)) if m else None


def _validated(raw_findings, by_id, answer, engine) -> list[dict]:
    out = []
    for raw in raw_findings:
        src = by_id.get(raw.source_id)
        if src is None or not src["blocks"]:
            continue
        anchor = locate_in_blocks(raw.page_quote, src["blocks"], prefer_block=_block_hint(raw.block_id))
        span = locate(raw.answer_quote, answer)
        if anchor is None or span is None:
            continue
        start, end = span
        suggestion = None
        after = raw.suggested_edit.strip()
        if src["owner"] == OWNER_YOU and after and after.lower() != anchor["text"].lower():
            suggestion = {"before": anchor["text"], "after": after,
                          "why": raw.edit_reason.strip() or "Clearer wording for AI answers."}
        out.append({
            "confidence": "confirmed" if _engine_overlap(engine, src, start, end) else "likely",
            "answer_quote": answer[start:end],
            "answer_start": start,
            "answer_end": end,
            "page_quote": anchor["text"],
            "page_anchor": {k: anchor[k] for k in ("block", "item", "start", "end")},
            "source": _source_payload(src),
            "reason": raw.reason.strip() or "The passage supports this part of the answer.",
            "suggestion": suggestion,
        })
    return out


def _engine_only(engine, sources, answer, existing) -> list[dict]:
    """Confirmed findings straight from engine-cited passages with a quoted
    passage, for answer spans the analysis did not already cover."""
    out = []
    for p in engine:
        cited = (p.get("cited_text") or "").strip()
        if not cited:
            continue
        src = _source_by_url(sources, p.get("url"))
        if src is None:
            continue
        start, end = p["answer_start"], p["answer_end"]
        covered = any(
            f["source"]["url"] == src["url"] and f["answer_start"] < end and start < f["answer_end"]
            for f in existing + out
        )
        if covered:
            continue
        anchor = locate_in_blocks(cited, src["blocks"]) if src["blocks"] else None
        out.append({
            "confidence": "confirmed",
            "answer_quote": answer[start:end],
            "answer_start": start,
            "answer_end": end,
            "page_quote": anchor["text"] if anchor else cited,
            "page_anchor": {k: anchor[k] for k in ("block", "item", "start", "end")} if anchor else None,
            "source": _source_payload(src),
            "reason": ENGINE_REASON,
            "suggestion": None,
        })
    return out


def _finalize(candidates: list[dict]) -> list[dict]:
    """Non-overlapping findings (confirmed ones win), in answer order,
    numbered f1, f2, ..."""
    ordered = sorted(candidates, key=lambda f: (f["confidence"] != "confirmed", f["answer_start"]))
    kept: list[dict] = []
    for f in ordered:
        if any(f["answer_start"] < k["answer_end"] and k["answer_start"] < f["answer_end"] for k in kept):
            continue
        kept.append(f)
    kept.sort(key=lambda f: f["answer_start"])
    return [{"id": f"f{i}", **f} for i, f in enumerate(kept, start=1)]


def _summary(result, website, findings) -> dict:
    rank = result.mention_rank if result.is_mentioned else None
    ahead = []
    if rank:
        for c in result.competitors_mentioned or []:
            if not isinstance(c, dict) or c.get("kind") == "generic":
                continue
            pos = c.get("position")
            if isinstance(pos, int) and pos < rank and c.get("name"):
                ahead.append((pos, c["name"]))
    ahead.sort()
    urls = {}
    for f in findings:
        urls.setdefault(f["source"]["url"], f["source"])
    by_type: dict[str, dict] = {}
    for src in urls.values():
        entry = by_type.setdefault(src["type_label"], {"type": src["type"], "label": src["type_label"],
                                                       "count": 0})
        entry["count"] += 1
    return {
        "brand": website.name or "",
        "mentioned": bool(result.is_mentioned),
        "rank": rank,
        "brands_ahead": [name for _, name in ahead][:5],
        "sources_total": len(urls),
        "sources_yours": sum(1 for s in urls.values() if s["owner"] == OWNER_YOU),
        "confirmed": sum(1 for f in findings if f["confidence"] == "confirmed"),
        "likely": sum(1 for f in findings if f["confidence"] == "likely"),
        "by_type": sorted(by_type.values(), key=lambda e: (-e["count"], e["label"])),
    }


def _user(user_id):
    if not user_id:
        return None
    from django.contrib.auth import get_user_model

    return get_user_model().objects.filter(pk=user_id).first()
