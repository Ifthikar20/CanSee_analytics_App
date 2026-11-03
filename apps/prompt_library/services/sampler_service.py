"""Stratified sampler — produces a reproducible per-audit prompt sample.

The sampler always persists a :class:`PromptSampleRun` plus
:class:`PromptSampleEntry` rows, capturing the seed used so the same
sample can be replayed. Strategies:

* ``stratified`` — equal slots per :class:`IntentBucket`, with overflow
  buckets back-filling under-quota buckets.
* ``recent``     — newest active prompts first.
* ``top_demand`` — highest source score first (the stored ranking score;
  the strategy keeps its API name, there is no demand signal).
"""
from __future__ import annotations

import random
from collections.abc import Iterable

from django.db import transaction

from apps.prompt_library.models import (
    IntentBucket,
    Prompt,
    PromptSampleEntry,
    PromptSampleRun,
)


def _stratified(prompts: list[Prompt], n: int, rng: random.Random) -> list[Prompt]:
    """Equal slots per bucket, then back-fill from leftovers."""
    buckets: dict[str, list[Prompt]] = {b.value: [] for b in IntentBucket}
    for p in prompts:
        buckets.setdefault(p.intent_bucket, []).append(p)
    for items in buckets.values():
        rng.shuffle(items)

    bucket_count = len(buckets) or 1
    quota = max(1, n // bucket_count)
    chosen: list[Prompt] = []
    leftovers: list[Prompt] = []
    for items in buckets.values():
        chosen.extend(items[:quota])
        leftovers.extend(items[quota:])
    if len(chosen) < n:
        rng.shuffle(leftovers)
        chosen.extend(leftovers[: n - len(chosen)])
    return chosen[:n]


def _recent(prompts: Iterable[Prompt], n: int) -> list[Prompt]:
    return sorted(prompts, key=lambda p: p.created_at, reverse=True)[:n]


def _top_demand(prompts: Iterable[Prompt], n: int) -> list[Prompt]:
    return sorted(prompts, key=lambda p: p.demand_score, reverse=True)[:n]


@transaction.atomic
def sample_prompts_for_audit(
    audit_run,
    industry,
    n: int = 50,
    strategy: str = "stratified",
    seed: int | None = None,
) -> PromptSampleRun:
    """Persist a sample of ``n`` prompts for the audit and return it.

    Re-running with the same ``(audit_run, industry, seed, strategy, n)``
    produces the same ordering — the rng is seeded explicitly.
    """
    if seed is None:
        seed = random.randint(0, 2**31 - 1)
    rng = random.Random(seed)

    pool = list(
        Prompt.objects.filter(industry=industry, is_active=True).only(
            "id", "text", "intent_bucket", "source", "demand_score", "created_at"
        )
    )

    if strategy == "recent":
        chosen = _recent(pool, n)
    elif strategy == "top_demand":
        chosen = _top_demand(pool, n)
    else:
        chosen = _stratified(pool, n, rng)

    # Drop any prior sample for the same audit so a re-run replaces it.
    PromptSampleRun.objects.filter(audit_run=audit_run).delete()
    sample_run = PromptSampleRun.objects.create(
        audit_run=audit_run, industry=industry, seed=seed, strategy=strategy
    )
    PromptSampleEntry.objects.bulk_create(
        [
            PromptSampleEntry(sample_run=sample_run, prompt=p, rank=i)
            for i, p in enumerate(chosen)
        ]
    )
    return sample_run
