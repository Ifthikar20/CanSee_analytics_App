"""
GEO domain tagger.

Tags user prompts with one of seven domains from Aggarwal et al.'s
"Generative Engine Optimization" paper (KDD '24, arXiv:2311.09735) and
surfaces the GEO tactic with the strongest published lift for that
domain. Categories and per-domain winning tactics come from Table 3
of the paper.

The tagger batches prompts into a single OpenAI call. Falls back to a
keyword heuristic when no API key is configured or the LLM call fails,
so the UI always renders something. Results are cached per prompt text
via the Django cache (hash key) to keep cost low across audit reloads.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re

from django.conf import settings
from django.core.cache import cache

from core.llm import OpenAIUtility

logger = logging.getLogger("apps")


# 7 GEO domain categories from the paper's GEO-bench tagging scheme.
GEO_CATEGORIES = (
    "debate",
    "facts",
    "law_gov",
    "people_society",
    "explanation",
    "history",
    "opinion",
)

CATEGORY_LABELS = {
    "debate":         "Debate",
    "facts":          "Facts",
    "law_gov":        "Law & Government",
    "people_society": "People & Society",
    "explanation":    "Explanation",
    "history":        "History",
    "opinion":        "Opinion",
}

# Tactic catalogue. Lifts (percent) are Position-Adjusted Word Count
# improvements vs. baseline from Table 1 of the paper.
TACTICS = {
    "quotation_addition": {
        "label":   "Add quotations",
        "lift":    41,
        "summary": "Insert short, attributed quotes from credible sources into the copy.",
    },
    "statistics_addition": {
        "label":   "Add statistics",
        "lift":    32,
        "summary": "Replace qualitative claims with concrete numbers, percentages and dates.",
    },
    "cite_sources": {
        "label":   "Cite sources",
        "lift":    30,
        "summary": "Add inline citations linking each non-obvious claim to a credible source.",
    },
    "authoritative": {
        "label":   "Authoritative tone",
        "lift":    11,
        "summary": "Tighten hedging and rewrite in a confident, expert voice.",
    },
    "fluency_optimization": {
        "label":   "Improve fluency",
        "lift":    28,
        "summary": "Smooth awkward phrasing; vary sentence length; remove filler.",
    },
    "easy_to_understand": {
        "label":   "Easy to understand",
        "lift":    14,
        "summary": "Simplify the language so a general reader can skim it in one pass.",
    },
    "technical_terms": {
        "label":   "Use technical terms",
        "lift":    18,
        "summary": "Introduce precise domain vocabulary the model can latch onto.",
    },
}

# Domain -> ranked list of recommended tactic IDs.
# Top-3 ranking follows Table 3 of the paper (best-performing tactic
# per category). Where the paper only published the rank-1 winner for
# a category we fall in behind it with the next-best universal lifts.
DOMAIN_RECOMMENDATIONS = {
    "debate": [
        "authoritative",
        "quotation_addition",
        "statistics_addition",
    ],
    "facts": [
        "cite_sources",
        "statistics_addition",
        "quotation_addition",
    ],
    "law_gov": [
        "statistics_addition",
        "cite_sources",
        "authoritative",
    ],
    "people_society": [
        "quotation_addition",
        "cite_sources",
        "fluency_optimization",
    ],
    "explanation": [
        "quotation_addition",
        "easy_to_understand",
        "fluency_optimization",
    ],
    "history": [
        "authoritative",
        "quotation_addition",
        "cite_sources",
    ],
    "opinion": [
        "statistics_addition",
        "quotation_addition",
        "authoritative",
    ],
}


def recommendations_for(category: str) -> list[dict]:
    """Return the ordered list of recommended tactics for a category."""
    ids = DOMAIN_RECOMMENDATIONS.get(category) or DOMAIN_RECOMMENDATIONS["facts"]
    out = []
    for i, tid in enumerate(ids):
        t = TACTICS.get(tid)
        if not t:
            continue
        out.append({
            "id":      tid,
            "label":   t["label"],
            "lift":    t["lift"],
            "summary": t["summary"],
            "rank":    i + 1,
        })
    return out


def _cache_key(prompt_text: str) -> str:
    h = hashlib.sha1(prompt_text.strip().lower().encode("utf-8")).hexdigest()
    return f"geo_tag:v1:{h}"


# Cheap keyword heuristic. Used as the LLM fallback so the UI never has
# an empty tag. Keep terms short and unambiguous; the LLM path handles
# anything subtler.
_HEURISTIC_RULES = (
    ("law_gov",        re.compile(r"\b(law|legal|regulat|gdpr|compliance|tax|govern|policy|sec\b)", re.I)),
    ("history",        re.compile(r"\b(history|historical|founded|origin|18\d{2}|19\d{2}|20\d{2}|when did)", re.I)),
    ("debate",         re.compile(r"\b(vs\.?|versus|better than|alternative|or\s+\w+\?|debate|pros and cons)", re.I)),
    ("opinion",        re.compile(r"\b(best|worst|should|recommend|review|opinion|favourite|favorite)", re.I)),
    # "generation" alone is too broad (lead generation, retrieval augmented
    # generation, power generation): only the demographic senses count.
    ("people_society", re.compile(
        r"\b(culture|society|people|community|demographic|generational"
        r"|generations?\s+(?:z|x|y|alpha)\b|gen\s?z\b|millennials?|boomers?)", re.I)),
    ("explanation",    re.compile(r"\b(how (do|does|to)|why|what is|explain|guide|tutorial)", re.I)),
)


def _heuristic_category(prompt_text: str) -> str:
    for cat, pattern in _HEURISTIC_RULES:
        if pattern.search(prompt_text):
            return cat
    return "facts"


def _build_batch_prompt(prompts: list[str]) -> str:
    numbered = "\n".join(f"{i + 1}. {p}" for i, p in enumerate(prompts))
    return (
        "You are tagging user search prompts for a Generative Engine "
        "Optimization analysis. For each prompt, choose ONE category "
        "from this set, using the snake_case id exactly:\n\n"
        "  debate           — comparison / 'vs' / which is better\n"
        "  facts            — verifiable, specific factual lookup\n"
        "  law_gov          — laws, regulation, government, compliance, tax\n"
        "  people_society   — culture, demographics, communities\n"
        "  explanation      — how / why / 'what is' / guides / tutorials\n"
        "  history          — historical events, origins, founding dates\n"
        "  opinion          — best / worst / should / recommendation queries\n\n"
        f"Prompts:\n{numbered}\n\n"
        "Return ONLY valid JSON of the form "
        '{"tags": [{"i": 1, "category": "facts"}, ...]} '
        "with one entry per prompt, no other text."
    )


def _parse_batch_response(text: str, count: int) -> list[str] | None:
    try:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        data = json.loads(match.group())
        tags = data.get("tags") or []
        out = ["facts"] * count
        for entry in tags:
            i = entry.get("i")
            cat = entry.get("category", "").strip()
            if isinstance(i, int) and 1 <= i <= count and cat in GEO_CATEGORIES:
                out[i - 1] = cat
        return out
    except (json.JSONDecodeError, AttributeError, TypeError) as e:
        logger.warning("GEO tag parse failed: %s", e)
        return None


def _llm_tag(prompts: list[str]) -> list[str] | None:
    api_key = getattr(settings, "OPENAI_API_KEY", "")
    if not api_key or not prompts:
        return None
    # Central LLM gateway handles rate-limit/breaker and usage recording
    # (role=geo_tagger). No user attribution here — same as before: the
    # tagger runs cache-side without a request user in scope.
    result = OpenAIUtility(model="gpt-4o-mini", max_tokens=512).query(
        _build_batch_prompt(prompts),
        role="geo_tagger",
        module="llm_ranking",
    )
    if not result.succeeded:
        logger.warning("GEO tagger LLM call failed: %s", result.error)
        return None
    return _parse_batch_response(result.text, len(prompts))


def tag_prompts(prompt_texts: list[str]) -> dict[str, dict]:
    """
    Tag a list of prompt texts. Returns a dict keyed by the original
    prompt text:

        {
            "<prompt>": {
                "category":        "facts",
                "category_label":  "Facts",
                "source":          "llm" | "heuristic" | "cache",
                "recommendations": [{id, label, lift, summary, rank}, ...],
            },
            ...
        }

    Cached entries short-circuit the LLM. Falls back to the keyword
    heuristic for any prompt the LLM can't tag.
    """
    if not prompt_texts:
        return {}

    # Deduplicate while preserving order.
    seen = {}
    for p in prompt_texts:
        if isinstance(p, str) and p.strip():
            seen.setdefault(p, None)
    unique = list(seen.keys())

    result: dict[str, dict] = {}
    to_query: list[str] = []

    for p in unique:
        cached = cache.get(_cache_key(p))
        if cached and cached in GEO_CATEGORIES:
            result[p] = _build_entry(cached, source="cache")
        else:
            to_query.append(p)

    if to_query:
        # Cap batch size — LLM token budgets and parse reliability both
        # drop on very large lists. Batches above ~50 also waste cache
        # hits for users on a typical 20–40 prompt audit.
        BATCH = 40
        for i in range(0, len(to_query), BATCH):
            chunk = to_query[i:i + BATCH]
            tags = _llm_tag(chunk)
            for j, prompt in enumerate(chunk):
                cat = tags[j] if tags else _heuristic_category(prompt)
                source = "llm" if tags else "heuristic"
                cache.set(_cache_key(prompt), cat, timeout=60 * 60 * 24 * 30)
                result[prompt] = _build_entry(cat, source=source)

    return result


def _build_entry(category: str, *, source: str) -> dict:
    return {
        "category":        category,
        "category_label":  CATEGORY_LABELS.get(category, category.title()),
        "source":          source,
        "recommendations": recommendations_for(category),
    }
