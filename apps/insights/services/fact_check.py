"""Fact check (contract 3d): the factual claims the engines make about the
brand, and what the brand's own evidence says about each.

Claims come from the current-version analyses of the window's answers and
are grouped by (normalized claim text, verdict). With no analyzed answer in
the window the counts are null (the page shows ``Not analyzed``), never 0.
"""

from __future__ import annotations

from apps.citations.services.url_analytics import model_key
from apps.insights.models import AnswerAnalysis, AnswerClaim, EvidencePage
from apps.insights.services import answer_analysis as aa
from apps.insights.services import evidence as evidence_service
from apps.insights.services import runs
from apps.insights.services.answer_prompts import TOPIC_KEYS, TOPIC_LABELS
from apps.llm_ranking.services import answer_window
from apps.search_gap.services.google_rank import is_same_site, site_apex

VERDICTS = ("supported", "contradicted", "not_verifiable", "unchecked")
SERIES_VERDICTS = ("supported", "contradicted", "not_verifiable")
# Table order: what needs attention first.
VERDICT_ORDER = {"contradicted": 0, "not_verifiable": 1, "unchecked": 2, "supported": 3}
MAX_CLAIMS = 200
MAX_OCCURRENCES = 5
MAX_CITED_URLS = 20
# What the payloads read of a claim and its relations: the answer's public
# id, never its text (a 90-day window holds thousands of claims, each of
# which would otherwise carry its whole answer).
CLAIM_FIELDS = (
    "analysis", "prompt", "evidence_fact", "text", "text_key", "topic", "quote", "verdict",
    "evidence_kind", "evidence_url", "evidence_quote", "note", "engine", "answered_at",
    "analysis__result__public_id", "prompt__text", "evidence_fact__subject",
    "evidence_fact__predicate", "evidence_fact__object", "evidence_fact__source_url",
)


def _claims(result_ids: list) -> list:
    rows = []
    for offset in range(0, len(result_ids), 1000):
        rows.extend(
            AnswerClaim.objects.filter(
                analysis__result_id__in=result_ids[offset:offset + 1000],
                analysis__version=aa.ANALYSIS_VERSION,
                analysis__status=AnswerAnalysis.STATUS_DONE,
            ).select_related("analysis__result", "evidence_fact", "prompt").only(*CLAIM_FIELDS)
        )
    return rows


def _evidence(row) -> dict | None:
    if row.evidence_kind == "page":
        return {"kind": "page", "url": row.evidence_url, "quote": row.evidence_quote, "fact": None}
    if row.evidence_kind == "fact":
        fact = row.evidence_fact
        return {
            "kind": "fact",
            "url": row.evidence_url or None,
            "quote": row.evidence_quote,
            "fact": {
                "id": str(fact.id), "subject": fact.subject, "predicate": fact.predicate,
                "object": fact.object, "source_url": fact.source_url,
            } if fact is not None else None,
        }
    return None


def group_claims(rows: list) -> list[dict]:
    """Claim objects (contract 3d) of ``rows``, in table order."""
    groups: dict = {}
    for row in rows:
        groups.setdefault((row.text_key, row.verdict), []).append(row)
    out = []
    for (key, verdict), items in groups.items():
        items.sort(key=lambda r: r.answered_at, reverse=True)
        newest = items[0]
        prompts, seen_prompts = [], set()
        for r in items:
            if r.prompt_id and r.prompt_id not in seen_prompts:
                seen_prompts.add(r.prompt_id)
                prompts.append({"prompt_id": str(r.prompt_id), "text": r.prompt.text})
        engines = {r.engine for r in items}
        out.append({
            "key": f"{key}|{verdict}",
            "text": newest.text,
            "topic": newest.topic,
            "verdict": verdict,
            "answers": len({r.analysis_id for r in items}),
            "engines": [e for e in answer_window.ENGINE_KEYS if e in engines],
            "prompts": prompts,
            "occurrences": [
                {"result_id": str(r.analysis.result.public_id), "engine": r.engine,
                 "answered_at": runs.iso(r.answered_at), "quote": r.quote}
                for r in items[:MAX_OCCURRENCES]
            ],
            "evidence": _evidence(newest),
            "note": newest.note,
            "last_seen_at": runs.iso(newest.answered_at),
            "_last_seen": newest.answered_at,
        })
    out.sort(key=lambda c: (VERDICT_ORDER.get(c["verdict"], 9), -c["answers"],
                            -c["_last_seen"].timestamp(), c["text"]))
    for claim in out:
        del claim["_last_seen"]
    return out


def _counts(rows) -> dict:
    out = {"claims": 0, **{v: 0 for v in VERDICTS}}
    for row in rows:
        out["claims"] += 1
        if row.verdict in out:
            out[row.verdict] += 1
    return out


def evidence_block(website) -> dict:
    """``{facts, facts_pending, pages}``: the current facts and the latest
    reads of the planned evidence pages."""
    counts = evidence_service.fact_counts(website)
    planned = evidence_service.planned_pages(website, fetch=False)
    reads = {p.url: p for p in EvidencePage.objects.filter(website=website, url__in=planned)}
    pages = []
    for url in planned:
        page = reads.get(url)
        if page is not None:
            ok = not page.error_code and bool(page.text)
            pages.append({"url": url, "state": "ok" if ok else "failed",
                          "read_at": runs.iso(page.read_at)})
    return {**counts, "pages": pages}


def build(website, *, period="30d", interval=None, engine=None, prompt_id=None, verdict=None,
          topic=None) -> dict:
    """The Fact check payload (contract 3d). ``verdict`` and ``topic`` filter
    the claims list; the counts follow the period, engine and prompt."""
    period, interval, engine, prompt_uuid = answer_window.parse_filters(
        period=period, interval=interval, engine=engine, prompt_id=prompt_id,
    )
    brand_prompts = aa.tracked_prompts(website)
    selected = aa.selected_prompt(brand_prompts, prompt_uuid)
    window, current, _previous = aa.window_answers(
        website, brand_prompts, period=period, interval=interval, engine=engine,
        prompt_bp=selected,
    )
    n = len(window.buckets)
    configured = aa.engine_configured()
    shown = aa.shown_engines(configured, current, engine)

    named = [(bp, r, i) for bp, r, i in current if r.is_mentioned]
    named_results = [r for _bp, r, _i in named]
    status = aa.analysis_status(r.id for r in named_results)
    cover = aa.coverage(named_results, status)
    done = [r.id for r in named_results if status.get(r.id) == AnswerAnalysis.STATUS_DONE]
    rows = _claims(done)
    bucket_of = {r.id: i for _bp, r, i in named}

    if not named:
        empty_reason = "no_answers"
    elif not cover["analyzed"]:
        empty_reason = "not_analyzed"
    elif not rows:
        empty_reason = "no_claims"
    else:
        empty_reason = None

    measured = cover["analyzed"] > 0
    totals = series = by_topic = by_engine = None
    if measured:
        totals = _counts(rows)
        series = {v: [0] * n for v in SERIES_VERDICTS}
        for row in rows:
            index = bucket_of.get(row.analysis.result_id)
            if index is not None and row.verdict in series:
                series[row.verdict][index] += 1
        by_topic = []
        for key in TOPIC_KEYS:
            topic_rows = [r for r in rows if r.topic == key]
            if topic_rows:
                by_topic.append({"topic": key, "label": TOPIC_LABELS[key], **_counts(topic_rows)})
        analyzed_engines = {
            model_key(r.provider) for r in named_results
            if status.get(r.id) == AnswerAnalysis.STATUS_DONE
        }
        by_engine = [
            {"key": key, **_counts([r for r in rows if r.engine == key])}
            for key in shown if key in analyzed_engines
        ]

    listed = [r for r in rows if (verdict is None or r.verdict == verdict)
              and (topic is None or r.topic == topic)]
    return {
        "window": aa.window_payload(window),
        "filters": {"engine": engine, "prompt_id": str(prompt_uuid) if prompt_uuid else None,
                    "verdict": verdict, "topic": topic},
        "engines": aa.engines_payload(configured),
        "prompts": [{"prompt_id": str(bp.prompt_id), "text": bp.prompt.text} for bp in brand_prompts],
        "empty_reason": empty_reason,
        "coverage": cover,
        "evidence": evidence_block(website),
        "totals": totals,
        "series": series,
        "by_topic": by_topic,
        "by_engine": by_engine,
        "claims": group_claims(listed)[:MAX_CLAIMS],
        **runs.run_state(website, aa.KEY),
    }


def claim_group(website, key: str, *, period: str = "90d") -> dict | None:
    """One claim of the Fact check (contract 3d) by its ``key``, plus
    ``cited_urls``: the pages the answers that made it cited, the project's
    own site left out, most cited first. None when no window answer makes it."""
    from apps.citations.models import Citation

    text_key, _sep, verdict = str(key or "").partition("|")
    if not text_key or not verdict:
        return None
    brand_prompts = aa.tracked_prompts(website)
    _window, current, _previous = aa.window_answers(website, brand_prompts, period=period)
    named = [r for _bp, r, _i in current if r.is_mentioned]
    status = aa.analysis_status(r.id for r in named)
    done = [r.id for r in named if status.get(r.id) == AnswerAnalysis.STATUS_DONE]
    rows = [r for r in _claims(done) if r.text_key == text_key and r.verdict == verdict]
    if not rows:
        return None
    claim = group_claims(rows)[0]

    apex = site_apex(website.url)
    result_ids = list({r.analysis.result_id for r in rows})
    cited: dict = {}
    for row in Citation.objects.filter(result_id__in=result_ids).values(
            "result_id", "url", "normalized_url", "domain", "is_target"):
        if row["is_target"] or is_same_site(row["domain"], apex):
            continue
        entry = cited.setdefault(row["normalized_url"], {
            "url": row["url"], "domain": row["domain"], "answers": set(),
        })
        entry["answers"].add(row["result_id"])
    ranked = sorted(cited.values(), key=lambda e: (-len(e["answers"]), e["url"]))
    claim["cited_urls"] = [
        {"url": e["url"], "domain": e["domain"], "answers": len(e["answers"])}
        for e in ranked[:MAX_CITED_URLS]
    ]
    return claim
