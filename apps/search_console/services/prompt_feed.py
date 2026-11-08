"""
Feed top GSC queries into the Prompt Library as GSC_AGGREGATE prompts.

Real Google demand is the strongest prompt signal we have:
scoring_service.SOURCE_WEIGHTS already gives gsc_aggregate the highest
weight (1.2), so anything created here is preferentially sampled into
audits once compute-demand-scores runs.

Rows created here are private to the website (owner_website, not in the
shared catalog): even unbranded queries can name competitors or places
the tenant cares about. Branded queries are still skipped. A query that
matches an existing row only raises that row's score.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import timedelta
from urllib.parse import urlparse

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

from apps.search_console.models import GscQueryStat

logger = logging.getLogger("apps")

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _resolve_industry(website):
    """Match website.industry to an Industry row (slug, then name)."""
    from apps.prompt_library.models import Industry

    slug = (website.industry or "").strip().lower()
    if not slug:
        return None
    return (
        Industry.objects.filter(is_active=True, slug__iexact=slug).first()
        or Industry.objects.filter(is_active=True, name__iexact=website.industry).first()
    )


def _brand_markers(website) -> tuple[set[str], set[str]]:
    """Return (single tokens, phrases) that identify the tenant.

    Individual name words are too coarse ("Acme Analytics" would flag
    every query containing "analytics"), so single-token matching is
    limited to the domain label and the first word of the name, which
    is where the distinctive brand term almost always sits. The full
    name is matched as a phrase.
    """
    tokens: set[str] = set()
    phrases: set[str] = set()

    name_words = _TOKEN_RE.findall((website.name or "").lower())
    if name_words:
        if len(name_words[0]) >= 3:
            tokens.add(name_words[0])
        if len(name_words) > 1:
            phrases.add(" ".join(name_words))

    host = urlparse(website.url).hostname or ""
    host = host[4:] if host.startswith("www.") else host
    label = host.split(".")[0] if host else ""
    if len(label) >= 3:
        tokens.add(label)

    return tokens, phrases


def _is_branded(query: str, brand_tokens: set[str], brand_phrases: set[str]) -> bool:
    words = _TOKEN_RE.findall(query.lower())
    if set(words) & brand_tokens:
        return True
    normalized = " ".join(words)
    return any(phrase in normalized for phrase in brand_phrases)


def _impressions_to_demand(impressions: int) -> float:
    """Map 28-day impressions to a 0..1 demand score.

    Same log10 shape as miner_service._score_to_demand: 10 impressions
    stay visible, 10k does not saturate.
    """
    if impressions <= 0:
        return 0.1
    return max(0.1, min(1.0, math.log10(impressions + 1) / 4.0))


def feed_prompts_for_website(website, *, days: int = 28, top_n: int | None = None) -> dict:
    """Create/boost GSC_AGGREGATE prompts from the website's top queries."""
    from apps.prompt_library.models import Prompt, PromptSource
    from apps.prompt_library.services._hash import text_hash
    from apps.prompt_library.services.miner_service import _classify_intent

    industry = _resolve_industry(website)
    if industry is None:
        return {"skipped": "no_industry"}

    top_n = top_n or int(getattr(settings, "GSC_PROMPT_TOP_N", 50))
    min_impressions = int(getattr(settings, "GSC_PROMPT_MIN_IMPRESSIONS", 10))
    since = timezone.now().date() - timedelta(days=days)

    candidates = (
        GscQueryStat.objects.filter(website=website, date__gte=since)
        .values("query")
        .annotate(total_impressions=Sum("impressions"))
        .filter(total_impressions__gte=min_impressions)
        .order_by("-total_impressions")[:top_n]
    )

    brand_tokens, brand_phrases = _brand_markers(website)
    created = 0
    boosted = 0
    skipped = 0
    for row in candidates:
        query = (row["query"] or "").strip()
        if len(query.split()) < 3 or _is_branded(query, brand_tokens, brand_phrases):
            skipped += 1
            continue

        demand = _impressions_to_demand(int(row["total_impressions"]))
        prompt, was_created = Prompt.objects.get_or_create(
            industry=industry,
            text_hash=text_hash(query),
            defaults={
                "text": query,
                "intent_bucket": _classify_intent(query),
                "source": PromptSource.GSC_AGGREGATE,
                "source_url": "",
                "excerpt": "",
                "demand_score": demand,
                # A tenant's own Search Console queries: private to the
                # website, offered back to it as suggestions only.
                "owner_website": website,
            },
        )
        if was_created:
            created += 1
        elif demand > prompt.demand_score:
            # Existing prompt (any source): real Google demand only ever
            # raises the score, never lowers what another miner set.
            prompt.demand_score = demand
            prompt.save(update_fields=["demand_score", "updated_at"])
            boosted += 1

    result = {"created": created, "boosted": boosted, "skipped": skipped}
    logger.info("GSC prompt feed for website %s: %s", website.pk, result)
    return result
