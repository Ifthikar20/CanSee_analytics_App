"""
Pluggable prompt miner.

Each miner implements :class:`BaseMiner` and is invoked by
:func:`mine_for_industry`. Miners gracefully no-op (with a logger
warning) when their credentials are missing, so the daily Celery
task can run on a half-configured environment without erroring.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable

from django.conf import settings

from apps.prompt_library.models import IntentBucket, Prompt, PromptSource
from apps.prompt_library.services._hash import text_hash

logger = logging.getLogger("apps")


class BaseMiner:
    source: str = ""

    def fetch_questions(self, industry, limit: int) -> list[dict]:
        """Return a list of ``{text, source_url, intent_bucket}`` dicts.

        Subclasses must override. The miner is responsible for limiting
        the result count to ``limit`` and for picking an
        :class:`IntentBucket` value per row (default to ``CATEGORY``).
        """
        raise NotImplementedError


def _classify_intent(text: str) -> str:
    """Cheap rule-based intent bucketer. Good enough for the seed pass —
    the LLM-synth miner can override it explicitly."""
    t = (text or "").lower()
    if any(w in t for w in (" vs ", " versus ", "compare", "difference between", "alternative")):
        return IntentBucket.COMPARISON
    if any(w in t for w in ("near me", "in my area", "in ", "city", "local")):
        return IntentBucket.LOCAL
    if any(w in t for w in ("how do i", "how to", "fix", "problem", "error", "why does")):
        return IntentBucket.PROBLEM
    return IntentBucket.CATEGORY


# industry slug -> subreddits to mine. Curated from the live subreddit
# index; missing slugs fall back to a literal slug-as-subreddit attempt.
SUBREDDIT_MAP: dict[str, list[str]] = {
    "saas-crm": ["CRM", "salesforce", "hubspot"],
    "saas-analytics": ["analytics", "dataengineering", "ProductManagement"],
    "saas-devtools": ["devops", "kubernetes", "webdev"],
    "e-commerce-dtc": ["shopify", "ecommerce", "FulfillmentByAmazon"],
    "e-commerce-marketplaces": ["FulfillmentByAmazon", "AmazonSeller", "Etsy"],
    "education-edtech": ["edtech", "Teachers", "homeschool"],
    "financial-services": ["personalfinance", "investing", "FinancialPlanning"],
    "food-beverage": ["AskCulinary", "Cooking", "Coffee"],
    "healthcare-telemedicine": ["AskDocs", "medicine", "HealthInsurance"],
    "hospitality-travel": ["travel", "awardtravel", "solotravel"],
    "insurance": ["Insurance", "personalfinance"],
    "legal-services": ["legaladvice", "smallbusiness", "Entrepreneur"],
    "local-services-hvac": ["HVAC", "homeowners"],
    "local-services-health-wellness": ["fitness", "yoga", "running"],
    "local-services-home-services": ["HomeImprovement", "Plumbing", "DIY"],
    "manufacturing-industrial": ["Manufacturing", "engineering"],
    "media-publishing": ["podcasting", "newsletters", "youtubers"],
    "nonprofit": ["nonprofit", "fundraising"],
    "professional-services-consulting": ["consulting", "smallbusiness"],
    "real-estate": ["RealEstate", "FirstTimeHomeBuyer", "Mortgages"],
}


def _truncate(text: str, limit: int = 600) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut + "…"


class RedditMiner(BaseMiner):
    """Mine question-shaped posts from subreddits relevant to each industry.

    Uses Reddit's public ``.json`` endpoint — no auth required, just a
    User-Agent. Authenticated PRAW path can be added later when
    ``REDDIT_CLIENT_ID``/``_SECRET`` are set; for now the unauthenticated
    public endpoint is sufficient and rate-limit friendly under our
    daily-cron volumes.
    """

    source = PromptSource.REDDIT
    USER_AGENT = "cansee-prompt-miner/0.2"

    def fetch_questions(self, industry, limit: int) -> list[dict]:
        try:
            import requests
        except ImportError:  # pragma: no cover
            logger.warning("RedditMiner: requests not available")
            return []

        subreddits = SUBREDDIT_MAP.get(industry.slug) or [industry.slug.replace("-", "")]
        results: list[dict] = []
        per_sub = max(5, limit // max(1, len(subreddits)))

        for sub in subreddits:
            if len(results) >= limit:
                break
            try:
                resp = requests.get(
                    f"https://www.reddit.com/r/{sub}/top.json",
                    params={"limit": min(per_sub, 100), "t": "month"},
                    headers={"User-Agent": self.USER_AGENT},
                    timeout=10,
                )
            except Exception as exc:
                logger.warning("RedditMiner: GET r/%s failed: %s", sub, exc)
                continue
            if resp.status_code != 200:
                logger.warning("RedditMiner: r/%s -> HTTP %s", sub, resp.status_code)
                continue
            for child in resp.json().get("data", {}).get("children", []):
                if len(results) >= limit:
                    break
                d = child.get("data", {})
                title = (d.get("title") or "").strip()
                if not title:
                    continue
                # We want "question-shaped" posts. Question marks are the
                # cheapest signal; "how", "why", "best", "vs" catch the
                # ones that don't bother with punctuation.
                lower = title.lower()
                looks_like_question = (
                    "?" in title
                    or lower.startswith(("how ", "why ", "what ", "where ", "when ", "is ", "are ", "should "))
                    or " vs " in lower
                    or "best " in lower
                )
                if not looks_like_question:
                    continue
                selftext = _truncate(d.get("selftext") or "", 600)
                permalink = d.get("permalink", "") or ""
                results.append({
                    "text": title,
                    "excerpt": selftext,
                    "source_url": ("https://www.reddit.com" + permalink) if permalink else "",
                    "intent_bucket": _classify_intent(title),
                })
        return results


class SerpApiMiner(BaseMiner):
    """Pulls People-Also-Ask + related questions for a category query."""

    source = PromptSource.SERPAPI

    def fetch_questions(self, industry, limit: int) -> list[dict]:
        api_key = getattr(settings, "SERPAPI_KEY", "")
        if not api_key:
            logger.warning("SerpApiMiner: SERPAPI_KEY missing, no-op")
            return []
        try:
            import requests
        except ImportError:  # pragma: no cover
            return []
        results: list[dict] = []
        try:
            resp = requests.get(
                "https://serpapi.com/search.json",
                params={"q": industry.name, "engine": "google", "api_key": api_key},
                timeout=15,
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
            for paa in (data.get("related_questions") or []):
                q = (paa.get("question") or "").strip()
                if not q:
                    continue
                results.append({
                    "text": q,
                    "source_url": paa.get("link", "") or "",
                    "intent_bucket": _classify_intent(q),
                })
                if len(results) >= limit:
                    break
            for rs in (data.get("related_searches") or []):
                if len(results) >= limit:
                    break
                q = (rs.get("query") or "").strip()
                if q:
                    results.append({
                        "text": q,
                        "source_url": rs.get("link", "") or "",
                        "intent_bucket": _classify_intent(q),
                    })
        except Exception as exc:
            logger.warning("SerpApiMiner failed: %s", exc)
        return results


class LLMSynthMiner(BaseMiner):
    """Synthesises canonical prompts via the existing Anthropic provider.

    Lazy-imports the LLM ranking provider registry so a missing
    Anthropic key results in a graceful no-op.
    """

    source = PromptSource.LLM_SYNTH

    META_PROMPT = (
        "Generate {limit} short, natural-language questions a real person "
        "would ask an AI assistant when researching the {industry} category. "
        "Mix four intent buckets in roughly equal proportions: category, "
        "comparison, problem, local. Return strict JSON: a list of "
        '{{"text": str, "intent_bucket": "category|comparison|problem|local"}} objects.'
    )

    def fetch_questions(self, industry, limit: int) -> list[dict]:
        try:
            from apps.llm_ranking.providers import get_provider
        except Exception as exc:  # pragma: no cover
            logger.warning("LLMSynthMiner: provider registry unavailable: %s", exc)
            return []
        provider = None
        for name in ("claude", "gpt4"):
            try:
                provider = get_provider(name)
                if provider is not None:
                    break
            except Exception:
                continue
        if provider is None:
            logger.warning("LLMSynthMiner: no provider configured, no-op")
            return []
        prompt = self.META_PROMPT.format(limit=limit, industry=industry.name)
        try:
            result = provider.query(prompt, "", user=None, website=None, audit_id="prompt_library_synth")
            text = getattr(result, "text", "") or ""
        except Exception as exc:
            logger.warning("LLMSynthMiner provider call failed: %s", exc)
            return []
        return _parse_synth_json(text, limit)


def _parse_synth_json(text: str, limit: int) -> list[dict]:
    import json
    import re

    match = re.search(r"\[.*\]", text or "", re.DOTALL)
    if not match:
        return []
    try:
        items = json.loads(match.group())
    except Exception:
        return []
    out: list[dict] = []
    valid = {b.value for b in IntentBucket}
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        t = (item.get("text") or "").strip()
        bucket = item.get("intent_bucket") or IntentBucket.CATEGORY
        if bucket not in valid:
            bucket = IntentBucket.CATEGORY
        if t:
            out.append({"text": t, "source_url": "", "intent_bucket": bucket})
    return out


_REGISTRY = {
    "reddit": RedditMiner,
    "serpapi": SerpApiMiner,
    "llm_synth": LLMSynthMiner,
}


def mine_for_industry(
    industry,
    sources: Iterable[str] = ("reddit", "serpapi", "llm_synth"),
    limit: int = 200,
) -> dict:
    """Run each requested miner and persist new prompts.

    Returns a summary ``{source: count}`` dict. Exact duplicates (by
    ``text_hash``) are skipped silently. Ranking scores are computed
    nightly by :mod:`scoring_service`, not here.
    """
    summary: dict[str, int] = {}
    for source in sources:
        cls = _REGISTRY.get(source)
        if cls is None:
            continue
        miner = cls()
        try:
            rows = miner.fetch_questions(industry, limit=limit)
        except Exception as exc:
            logger.warning("Miner %s raised: %s", source, exc)
            continue
        created = 0
        for row in rows:
            text = (row.get("text") or "").strip()
            if not text:
                continue
            defaults = {
                "text": text,
                "intent_bucket": row.get("intent_bucket") or IntentBucket.CATEGORY,
                "source": miner.source,
                "source_url": row.get("source_url", "") or "",
                "excerpt": row.get("excerpt", "") or "",
                # Mined by ops from public sources: the shared catalog.
                "is_catalog": True,
            }
            obj, was_created = Prompt.objects.get_or_create(
                industry=industry,
                text_hash=text_hash(text),
                defaults=defaults,
            )
            if was_created:
                created += 1
            else:
                # Backfill excerpt + URL on existing rows that pre-date the field.
                changed = False
                if not obj.excerpt and defaults["excerpt"]:
                    obj.excerpt = defaults["excerpt"]
                    changed = True
                if not obj.source_url and defaults["source_url"]:
                    obj.source_url = defaults["source_url"]
                    changed = True
                if changed:
                    obj.save(update_fields=["excerpt", "source_url", "updated_at"])
        summary[source] = created
    return summary
