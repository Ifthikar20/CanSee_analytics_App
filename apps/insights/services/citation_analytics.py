"""Citation analytics (contract 4b): which sources the engines list in the
window's answers, and where you and your competitors are cited.

One ``Citation`` row is one source an answer listed. ``cited_in_text``
counts the rows the answer text points to (``reference_count > 0``);
``retrieved_only`` the rows the engine fetched but never pointed to. The
Sources table further down the same page counts differently (it sums
``reference_count`` as citations), which the page says.
"""

from __future__ import annotations

from collections import Counter

from apps.citations.models import Citation, SourceClass
from apps.citations.services.url_analytics import model_key
from apps.insights.services import answer_analysis as aa
from apps.insights.services import competitors as competitors_service
from apps.insights.services import runs
from apps.llm_ranking.services import answer_window
from apps.search_gap.services.google_rank import is_same_site, site_apex

MAX_PAGES = 25
MAX_GAPS = 15
MAX_GAP_DOMAINS = 3
CHUNK = 1000
CLASS_LABELS = dict(SourceClass.choices)


def _rows(result_ids: list) -> list[dict]:
    rows: list[dict] = []
    for offset in range(0, len(result_ids), CHUNK):
        rows.extend(
            Citation.objects.filter(result_id__in=result_ids[offset:offset + CHUNK]).values(
                "result_id", "url", "normalized_url", "domain", "apex_domain", "source_class",
                "reference_count", "is_target",
            )
        )
    return rows


def _engines(keys: set) -> list[str]:
    return [k for k in answer_window.ENGINE_KEYS if k in keys]


def build(website, *, period="30d", engine=None) -> dict:
    """The citation analytics payload (contract 4b)."""
    period, interval, engine, _prompt = answer_window.parse_filters(period=period, engine=engine)
    brand_prompts = aa.tracked_prompts(website)
    window, current, _previous = aa.window_answers(
        website, brand_prompts, period=period, interval=interval, engine=engine,
    )
    answer_of = {result.id: (bp, result) for bp, result, _i in current}
    rows = _rows(list(answer_of))
    configured = aa.engine_configured()
    shown = aa.shown_engines(configured, current, engine)
    window_payload = aa.window_payload(window)
    if not rows:
        return {
            "window": window_payload, "filters": {"engine": engine}, "has_data": False,
            "totals": None, "by_class": [], "by_engine": [], "your_pages": [],
            "competitor_pages": [], "gaps": [],
        }

    apex = site_apex(website.url)
    rivals = competitors_service.brands(website)[1:]
    by_apex = {b["apex"]: b for b in rivals if b["apex"]}
    classes: dict = {}
    engine_rows: dict = {}
    yours: dict = {}
    theirs: dict = {}
    per_prompt: dict = {}
    with_sources = set()
    for row in rows:
        bp, result = answer_of[row["result_id"]]
        key = model_key(result.provider)
        with_sources.add(row["result_id"])
        own = bool(row["is_target"]) or is_same_site(row["domain"], apex)
        rival = None if own else by_apex.get(row["apex_domain"] or "")

        cls = classes.setdefault(row["source_class"], {"citations": 0, "answers": set()})
        cls["citations"] += 1
        cls["answers"].add(row["result_id"])

        e = engine_rows.setdefault(key, {"answers": set(), "citations": 0, "yours": 0})
        e["answers"].add(row["result_id"])
        e["citations"] += 1
        e["yours"] += int(own)

        if own:
            page = yours.setdefault(row["normalized_url"], {
                "url": row["url"], "citations": 0, "cited_in_text": 0, "answers": set(),
                "engines": set(), "prompts": set(), "last": None,
            })
            page["citations"] += 1
            page["cited_in_text"] += int((row["reference_count"] or 0) > 0)
            page["answers"].add(row["result_id"])
            page["engines"].add(key)
            page["prompts"].add(bp.id)
            if page["last"] is None or result.created_at > page["last"]:
                page["last"] = result.created_at
        elif rival is not None:
            page = theirs.setdefault(row["normalized_url"], {
                "url": row["url"], "brand": rival, "citations": 0, "answers": set(),
                "engines": set(),
            })
            page["citations"] += 1
            page["answers"].add(row["result_id"])
            page["engines"].add(key)

        p = per_prompt.setdefault(bp.id, {"bp": bp, "yours": 0, "theirs": 0, "domains": Counter()})
        p["yours"] += int(own)
        p["theirs"] += int(rival is not None)
        p["domains"][row["apex_domain"] or row["domain"]] += 1

    answers_by_engine = Counter(model_key(result.provider) for _bp, result, _i in current)
    cited_in_text = sum(1 for row in rows if (row["reference_count"] or 0) > 0)
    your_pages = sorted(
        yours.values(), key=lambda p: (-p["citations"], -len(p["answers"]), p["url"]),
    )
    competitor_pages = sorted(
        theirs.values(), key=lambda p: (-p["citations"], -len(p["answers"]), p["url"]),
    )
    gaps = sorted(
        (p for p in per_prompt.values() if p["theirs"] and not p["yours"]),
        key=lambda p: (-p["theirs"], p["bp"].prompt.text),
    )
    return {
        "window": window_payload,
        "filters": {"engine": engine},
        "has_data": True,
        "totals": {
            "answers": len(current),
            "answers_with_sources": len(with_sources),
            "citations": len(rows),
            "cited_in_text": cited_in_text,
            "retrieved_only": len(rows) - cited_in_text,
            "your_pages": len(yours),
        },
        "by_class": [
            {"key": cls, "label": CLASS_LABELS.get(cls, cls), "citations": v["citations"],
             "answers": len(v["answers"])}
            for cls, v in sorted(classes.items(), key=lambda kv: (-kv[1]["citations"], kv[0]))
        ],
        "by_engine": [
            {
                "key": key,
                "label": aa.ENGINE_LABELS[key],
                "answers": answers_by_engine.get(key, 0),
                "answers_with_sources": len(engine_rows.get(key, {}).get("answers", ())),
                "citations": engine_rows.get(key, {}).get("citations", 0),
                "your_citations": engine_rows.get(key, {}).get("yours", 0),
            }
            for key in shown
        ],
        "your_pages": [
            {"url": p["url"], "citations": p["citations"], "cited_in_text": p["cited_in_text"],
             "answers": len(p["answers"]), "engines": _engines(p["engines"]),
             "prompts": len(p["prompts"]), "last_cited_at": runs.iso(p["last"])}
            for p in your_pages[:MAX_PAGES]
        ],
        "competitor_pages": [
            {"url": p["url"], "brand_key": p["brand"]["key"], "brand_name": p["brand"]["name"],
             "citations": p["citations"], "answers": len(p["answers"]),
             "engines": _engines(p["engines"])}
            for p in competitor_pages[:MAX_PAGES]
        ],
        "gaps": [
            {"prompt_id": str(p["bp"].prompt_id), "text": p["bp"].prompt.text,
             "competitor_citations": p["theirs"], "your_citations": 0,
             "top_domains": [{"domain": d, "citations": c}
                             for d, c in p["domains"].most_common(MAX_GAP_DOMAINS)]}
            for p in gaps[:MAX_GAPS]
        ],
    }
