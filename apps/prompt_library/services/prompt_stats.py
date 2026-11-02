"""Per-prompt answer matching and per-engine roll-up.

Shared by the prompt detail view (``BrandPromptDetailAggView``) and the
search-gap service, so "which answers belong to this prompt" and "how did
each engine do on it" mean the same thing on every screen.
"""

from __future__ import annotations

from django.conf import settings
from django.db.models import Q

MODEL_LABELS = {
    "claude": "Claude", "gpt4": "ChatGPT", "gemini": "Gemini",
    "perplexity": "Perplexity", "grok": "Grok",
}


def normalise(text: str) -> str:
    return " ".join((text or "").split()).lower()


def _matches(result, prompt_id, norm_text: str, norm_template: str) -> bool:
    rn = normalise(result.prompt)
    return (
        result.source_prompt_id == prompt_id
        or rn == norm_text
        or bool(norm_template and rn == norm_template)
    )


def matching_results(website, prompt, *, since=None, fields=None) -> list:
    """Every LLMRankingResult on ``website`` that answered ``prompt``.

    Match by the source_prompt FK (set when the cell ran) OR by exact
    normalised text against the prompt's text or template. The FK is the
    reliable link: it catches region-flavored or whitespace-different
    prompt text that pure string matching would miss.
    """
    from apps.llm_ranking.models import LLMRankingResult

    norm_text = normalise(prompt.text)
    norm_template = normalise(prompt.template_text or "")
    candidates = (
        LLMRankingResult.objects
        .filter(audit__website=website)
        .filter(
            Q(source_prompt_id=prompt.id)
            | Q(prompt__icontains=prompt.text[:80])
            | Q(prompt__icontains=(prompt.template_text or "")[:80])
        )
    )
    if fields:
        candidates = candidates.only(*fields)
    if since is not None:
        candidates = candidates.filter(created_at__gte=since)
    return [r for r in candidates if _matches(r, prompt.id, norm_text, norm_template)]


def results_by_prompt(website, prompts, *, since=None, fields=None) -> dict:
    """``matching_results`` for many prompts in one query.

    Returns ``{prompt.id: [results]}``. Used by screens that list every
    tracked prompt, where one query per prompt would not scale.
    """
    from apps.llm_ranking.models import LLMRankingResult

    prompts = list(prompts)
    out: dict = {p.id: [] for p in prompts}
    if not prompts:
        return out
    by_id = {p.id: p for p in prompts}
    by_text: dict[str, list] = {}
    for p in prompts:
        for key in {normalise(p.text), normalise(p.template_text or "")} - {""}:
            by_text.setdefault(key, []).append(p.id)

    qs = LLMRankingResult.objects.filter(audit__website=website)
    if fields:
        qs = qs.only(*fields)
    if since is not None:
        qs = qs.filter(created_at__gte=since)
    for r in qs:
        if r.source_prompt_id in by_id:
            out[r.source_prompt_id].append(r)
            continue
        for pid in by_text.get(normalise(r.prompt), ()):
            out[pid].append(r)
    return out


def rank_buckets(ranks: list) -> dict:
    buckets = {"1": 0, "2-3": 0, "4-10": 0, "11+": 0}
    for rank in ranks:
        if rank == 1:
            buckets["1"] += 1
        elif rank <= 3:
            buckets["2-3"] += 1
        elif rank <= 10:
            buckets["4-10"] += 1
        else:
            buckets["11+"] += 1
    return buckets


def is_configured(provider_cls) -> bool:
    return bool(getattr(settings, provider_cls.api_key_setting, ""))


def aggregate_by_model(results) -> list[dict]:
    """Brand visibility per model for one prompt.

    Every implemented provider appears: unconfigured ones are flagged so
    the UI shows "Not configured" instead of a misleading 0%. Failed
    cells whose error_message starts with the service_unavailable
    sentinel are counted separately so the UI can show a distinct state
    for "we tried but the provider was down" versus "no data yet".
    """
    from apps.citations.services.url_analytics import model_key
    from apps.llm_ranking.providers import PROVIDERS

    per_model: dict[str, dict] = {}
    for r in results:
        m = per_model.setdefault(r.provider, {
            "responses": 0, "mentioned": 0, "unavailable": 0, "failed": 0,
            "ranks": [],
        })
        if not r.query_succeeded:
            err = getattr(r, "error_message", "") or ""
            if err.startswith("service_unavailable"):
                m["unavailable"] += 1
            else:
                m["failed"] += 1
            continue
        m["responses"] += 1
        if r.is_mentioned:
            m["mentioned"] += 1
            if r.mention_rank is not None:
                m["ranks"].append(r.mention_rank)

    by_model = []
    for key, cls in PROVIDERS.items():
        stat = per_model.get(key, {
            "responses": 0, "mentioned": 0, "unavailable": 0, "failed": 0,
            "ranks": [],
        })
        responses = stat["responses"]
        by_model.append({
            "provider": key,
            "model": model_key(key),
            "label": MODEL_LABELS.get(key, key),
            "configured": is_configured(cls),
            "responses": responses,
            "mentioned": stat["mentioned"],
            "unavailable": stat["unavailable"],
            "failed": stat.get("failed", 0),
            "visibility_pct": (
                round(100 * stat["mentioned"] / responses, 1) if responses else 0.0
            ),
            "rank_buckets": rank_buckets(stat["ranks"]),
            "avg_rank": (
                round(sum(stat["ranks"]) / len(stat["ranks"]), 1)
                if stat["ranks"] else None
            ),
        })
    # Configured-with-data first, then by visibility, unconfigured last.
    by_model.sort(key=lambda m: (not m["configured"], -m["responses"], -m["visibility_pct"]))
    return by_model
