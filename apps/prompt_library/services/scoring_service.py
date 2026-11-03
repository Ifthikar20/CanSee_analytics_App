"""Source score — the library's ranking score, refreshed nightly.

It is not a demand measurement: no search-volume or popularity signal
feeds it. The score blends three properties of the prompt row:

* source weight — primary-source mining (Reddit / SerpAPI / Search
  Console) ranks above pure LLM synthesis.
* recency      — exponentially decays with age in days.
* variation count — prompts with stored paraphrases rank higher.

The value lives in the ``Prompt.demand_score`` column for schema
compatibility and is exposed by the API as ``source_score``. It is
clipped to ``[0, 100]``.
"""
from __future__ import annotations

import math

from django.utils import timezone

from apps.prompt_library.models import Prompt, PromptSource

SOURCE_WEIGHTS = {
    PromptSource.REDDIT: 1.0,
    PromptSource.QUORA: 1.0,
    PromptSource.SERPAPI: 1.1,
    PromptSource.DATAFORSEO: 1.1,
    PromptSource.GSC_AGGREGATE: 1.2,
    PromptSource.LLM_SYNTH: 0.6,
    PromptSource.MANUAL: 0.8,
}

HALF_LIFE_DAYS = 30.0


def _recency(prompt: Prompt) -> float:
    if not prompt.created_at:
        return 0.5
    age_days = max(0.0, (timezone.now() - prompt.created_at).total_seconds() / 86400.0)
    return math.exp(-math.log(2) * age_days / HALF_LIFE_DAYS)


def compute_source_score(prompt: Prompt) -> float:
    weight = SOURCE_WEIGHTS.get(prompt.source, 0.5)
    recency = _recency(prompt)
    variation_count = prompt.variations.count() if prompt.pk else 0
    variation_bonus = math.log1p(variation_count) / math.log(10)  # 0..~1 for 0..9 vars
    raw = (weight * 50.0) + (recency * 30.0) + (variation_bonus * 20.0)
    return max(0.0, min(100.0, raw))


def refresh_all_scores(batch_size: int = 500) -> int:
    """Recompute and persist the source score for every active prompt."""
    qs = Prompt.objects.filter(is_active=True).only("id", "source", "created_at", "demand_score")
    updated = 0
    batch: list[Prompt] = []
    for prompt in qs.iterator():
        prompt.demand_score = compute_source_score(prompt)
        batch.append(prompt)
        if len(batch) >= batch_size:
            Prompt.objects.bulk_update(batch, ["demand_score"])
            updated += len(batch)
            batch = []
    if batch:
        Prompt.objects.bulk_update(batch, ["demand_score"])
        updated += len(batch)
    return updated
