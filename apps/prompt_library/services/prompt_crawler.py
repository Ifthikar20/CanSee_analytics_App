"""
Per-prompt crawler.

Given a (website, Prompt) pair, run the prompt across every configured
LLM provider and capture:

* The model's full response (stored on an ad-hoc LLMRankingAudit/Result
  row so the detail-page aggregation picks it up alongside any
  full-audit data). The audit is scored from its answers like a full
  run; one with no answered query ends FAILED with NULL scores.
* A list of fan-out sub-queries (PromptFanout rows). Claude decomposes
  the prompt into 4-8 buyer-intent sub-queries; without Claude the
  fan-out stays empty.

The whole thing is invoked synchronously from a Celery task; views
fire-and-forget via ``.delay()``. Failures on a single provider don't
fail the whole crawl — we record what we got.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from django.db.models import Q
from django.utils import timezone

from apps.llm_ranking.models import LLMRankingAudit, LLMRankingResult
from apps.llm_ranking.providers import (
    PROVIDERS,
    get_provider_for_variant,
    list_model_variants,
    parse_variant,
)
from apps.llm_ranking.services.ranking_service import (
    SCORE_FIELDS,
    LLMRankingService,
    _merge_citation_lists,
    citation_passage_fields,
    search_query_fields,
)
from apps.prompt_library.models import (
    BrandPrompt,
    Prompt,
    PromptCrawlRun,
    PromptFanout,
)
from apps.websites.models import Website

logger = logging.getLogger(__name__)


FANOUT_MAX = 8


def _dedupe_fanouts(items: list[str]) -> list[str]:
    """De-dupe sub-queries case-insensitively (preserving order) and cap at
    FANOUT_MAX so the stored set stays short and high-signal."""
    seen: set[str] = set()
    out: list[str] = []
    for q in items:
        key = " ".join((q or "").split()).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(q.strip())
        if len(out) >= FANOUT_MAX:
            break
    return out


@dataclass
class CrawlOutcome:
    fanouts: list[str]
    responses: int
    errors: list[str]
    # should_stop asked the crawl to end before every model was asked.
    stopped: bool = False


def _llm_fanout(
    prompt_text: str, brand_name: str, *, user=None, website=None, actor: str = "system",
) -> list[str]:
    """Ask Claude to decompose the prompt into buyer-intent sub-queries.

    Returns only genuine LLM-generated sub-queries. On any failure it
    returns an empty list rather than padding with templated queries -
    an empty, honest fan-out beats a list bloated with mechanical
    "best X / pricing for X / X vs competitors" rows.
    """
    try:
        # Lazy import — if Anthropic isn't installed or the key isn't
        # set, the provider raises on instantiation.
        from apps.llm_ranking.providers.claude import ClaudeProvider
        prov = ClaudeProvider()
    except Exception:
        return []

    system = (
        "You break a buyer-intent prompt into 4-8 short sub-queries an "
        "AI search engine would run in parallel to gather context. "
        "Return only a JSON array of strings, no commentary."
    )
    user_prompt = f"Brand: {brand_name}\nPrompt: {prompt_text}"
    try:
        resp = prov.query(
            user_prompt,
            system_prompt=system,
            user=user,
            website=website,
            module="prompt_library",
            role="fanout",
            extra_metadata={"actor": actor},
        )
    except Exception as exc:
        logger.warning("Fanout LLM call failed: %s", exc)
        return []

    text = getattr(resp, "text", None) or str(resp)
    # Pull the first JSON array out of the response.
    match = re.search(r"\[[^\]]+\]", text or "", re.DOTALL)
    if not match:
        return []
    try:
        items = json.loads(match.group())
    except json.JSONDecodeError:
        return []
    cleaned = [str(x).strip() for x in items if isinstance(x, str | int | float) and str(x).strip()]
    return cleaned[:FANOUT_MAX]


def _website_keywords(website: Website) -> list[str]:
    """Keywords the extractor uses to recognise the brand. Combines the
    business name, configured topics, and competitor names so the LLM
    extractor has enough context to label mentions."""
    out: list[str] = []
    name = getattr(website, "business_name", None) or website.name or ""
    if name:
        out.append(name)
    for t in (getattr(website, "topics", None) or []):
        if isinstance(t, str) and t.strip():
            out.append(t.strip())
    for c in (getattr(website, "competitors", None) or []):
        cname = c.get("name") if isinstance(c, dict) else (c if isinstance(c, str) else None)
        if cname and cname.strip():
            out.append(cname.strip())
    # De-dupe, preserve order.
    seen, deduped = set(), []
    for k in out:
        kl = k.lower()
        if kl not in seen:
            seen.add(kl)
            deduped.append(k)
    return deduped


def _extract_brands(*, response_text, website, brand_name, keywords, audit_id, user=None) -> dict:
    """Run the same structured extraction the full audit uses so the
    crawl captures the brand, its rank/sentiment, and every competitor
    named in the response. Falls back to an empty analysis when there's
    no text or the extractor errors. ``user`` is the spend owner (the
    acting user for manual scans, the website owner for scheduled ones)."""
    if not (response_text or "").strip():
        from apps.llm_ranking.services.extraction_service import HaikuExtractionService
        return HaikuExtractionService._empty_result()
    try:
        from apps.llm_ranking.services.extraction_service import HaikuExtractionService
        return HaikuExtractionService.extract(
            response_text=response_text,
            brand_name=brand_name,
            keywords=keywords,
            user=user if user is not None else getattr(website, "user", None),
            website=website,
            audit_id=audit_id,
        )
    except Exception as exc:
        logger.warning("Crawl extraction failed: %s", exc)
        from apps.llm_ranking.services.extraction_service import HaikuExtractionService
        return HaikuExtractionService._empty_result()


def _query_with_retry(instance, prompt_text, *, user=None, website=None,
                      audit_id=None, actor: str = "system", extra_metadata=None,
                      run_feature: str = "prompt_crawl", run_key: str | None = None):
    """Query a provider, retrying once when it raises or returns a
    non-key transient failure. Returns the ProviderResult (which may
    still be succeeded=False) or None if both attempts raised.

    A succeeded=False result whose error is the not-configured /
    service_unavailable sentinel is returned immediately without a
    retry — a missing key won't fix itself on a second call. Neither is
    a refusal: it is the model's answer to this prompt, and asking again
    bills the same decline.

    The attribution kwargs make crawl spend visible and capped: an
    unattributed query() records nothing and bypasses the spend wall.
    ``extra_metadata`` adds flat tags to the usage row (Autopilot's
    ``feature`` and ``autopilot_run``); ``actor`` always wins.

    Both attempts together are one prompt run (``run_feature``, keyed by
    ``run_key``), counted only for a successful, non-empty answer. With
    no run left the engine is not asked and the result carries
    prompt_runs.DENIAL.
    """
    from apps.metering.services import prompt_runs

    return prompt_runs.guard(
        lambda: _ask_twice(instance, prompt_text, user=user, website=website,
                           audit_id=audit_id, actor=actor, extra_metadata=extra_metadata),
        user=user, website=website, engine=getattr(instance, "name", ""),
        feature=run_feature, key=run_key,
    )


def _ask_twice(instance, prompt_text, *, user, website, audit_id, actor, extra_metadata):
    tags = {**(extra_metadata or {}), "actor": actor}
    last = None
    for attempt in (1, 2):
        try:
            result = instance.query(
                prompt_text,
                user=user,
                website=website,
                audit_id=audit_id,
                module="prompt_library",
                extra_metadata=dict(tags),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Provider query raised (attempt %d): %s", attempt, exc)
            last = None
            continue
        last = result
        if getattr(result, "succeeded", True):
            return result
        err = (getattr(result, "error", "") or "").lower()
        if "service_unavailable" in err or "not configured" in err or "not enabled" in err:
            return result  # no point retrying a missing key
        if err.startswith("refusal:"):
            return result
        # transient failure — loop for one more attempt
    return last


def _dispatch_citations(result_id) -> None:
    """Kick off citation extraction so the Top Domains table populates.
    Mirrors the main audit flow; failures are non-fatal."""
    from django.conf import settings as _settings
    if not getattr(_settings, "CITATION_EXTRACTION_ENABLED", True):
        return
    try:
        from apps.citations.tasks import extract_citations_for_result
        extract_citations_for_result.delay(str(result_id))
    except Exception as exc:  # pragma: no cover
        logger.debug("citation dispatch failed for %s: %s", result_id, exc)


def _dispatch_alignment(result_id) -> None:
    """Kick off brand-alignment scoring for a successful response.
    Mirrors the main audit flow; failures are non-fatal."""
    from django.conf import settings as _settings
    if not getattr(_settings, "CLAIM_VERIFICATION_ENABLED", True):
        return
    try:
        from apps.brand_vault.tasks import analyze_alignment_for_result
        analyze_alignment_for_result.delay(str(result_id))
    except Exception as exc:  # pragma: no cover
        logger.debug("alignment dispatch failed for %s: %s", result_id, exc)


def _start_run(website: Website, prompt: Prompt, crawl_run_id=None) -> PromptCrawlRun:
    """Mark the reserved pending run as running, or create a running one."""
    now = timezone.now()
    if crawl_run_id:
        claimed = PromptCrawlRun.objects.filter(
            id=crawl_run_id, website=website, prompt=prompt,
            status=PromptCrawlRun.STATUS_PENDING,
        ).update(status=PromptCrawlRun.STATUS_RUNNING, started_at=now)
        if claimed:
            return PromptCrawlRun.objects.get(id=crawl_run_id)
    return PromptCrawlRun.objects.create(
        website=website, prompt=prompt,
        status=PromptCrawlRun.STATUS_RUNNING, started_at=now,
    )


def crawl_prompt(
    website: Website, prompt: Prompt, *, only_missing: bool = False,
    acting_user=None, should_stop=None, providers=None, run_feature: str | None = None,
    crawl_run_id=None,
) -> CrawlOutcome:
    """Run a prompt across every configured provider and persist the
    fanout + responses. Returns a small outcome summary used by the
    Celery task to update the PromptCrawlRun row.

    ``only_missing=True`` queries just the providers that have never
    answered this prompt (the page's silent gap-fill). The default is a
    FULL re-run of every configured model: manual "Run scan" and
    scheduled runs exist to capture a fresh answer set per run, so they
    must never skip a model just because it answered previously.

    ``acting_user`` is whoever clicked "Run scan"; spend is attributed
    to them (actor="user"). Scheduled/system runs leave it None and the
    spend goes to the website owner, tagged actor="system" so the usage
    breakdown can tell the two apart.

    ``should_stop`` (a callable, optional) is checked before each model is
    asked: when it returns True the crawl asks no further model and ends
    with what it has (``stopped`` on the outcome). An answer already
    received is still analysed and stored, so it is not paid for twice.

    ``providers`` (provider keys, optional) limits the crawl to those
    engines, e.g. the engines chosen at onboarding. Each answer is one
    prompt run of ``run_feature`` ("prompt_crawl" for a person,
    "scheduled_crawl" for the scheduler by default).

    ``crawl_run_id`` names the pending PromptCrawlRun that Check now
    reserved when it queued this crawl; it is reused so the slot that
    refuses a second click stays one row. A missing or no longer pending
    row (e.g. timed out on read) is replaced by a new one.
    """
    spend_user = acting_user or getattr(website, "user", None)
    only = {str(p) for p in providers} if providers else None
    if run_feature is None:
        run_feature = "prompt_crawl" if acting_user is not None else "scheduled_crawl"
    stopped = False

    def stop_requested() -> bool:
        return should_stop is not None and bool(should_stop())
    actor = "user" if acting_user is not None else "system"

    run = _start_run(website, prompt, crawl_run_id)

    fanouts: list[str] = []
    errors: list[str] = []
    responses_logged = 0

    brand_name = getattr(website, "business_name", None) or website.name or "your brand"
    keywords = _website_keywords(website)
    fanouts = _dedupe_fanouts(
        _llm_fanout(
            prompt.text or prompt.template_text or "", brand_name,
            user=spend_user, website=website, actor=actor,
        )
    )

    # Replace this prompt's fan-out set rather than appending, so re-scans
    # don't accumulate duplicates. Only write when we actually generated
    # fan-outs (a failed LLM call leaves the previous set untouched).
    if fanouts:
        PromptFanout.objects.filter(website=website, prompt=prompt).delete()
        PromptFanout.objects.bulk_create([
            PromptFanout(
                website=website,
                prompt=prompt,
                text=q,
                source=PromptFanout.SOURCE_CRAWLER,
            )
            for q in fanouts
        ])

    # Gap-fill mode: skip providers that already have a good answer, so
    # the page's automatic scan only queries what's missing. Full runs
    # (manual re-scan, scheduled runs) re-query everything — each run is
    # its own audit, so fresh rows extend the time series rather than
    # duplicating within a run.
    # Gap-fill bookkeeping. Keys are provider names for default-selection
    # runs and "provider:model_id" for prompts with an explicit model
    # selection, so "never answered" is judged at the granularity the
    # prompt is actually scanned at. Rows predating model_id tracking
    # count only at provider level.
    already_answered: set[str] = set()
    if only_missing:
        answered_rows = (
            LLMRankingResult.objects
            .filter(audit__website=website, query_succeeded=True)
            .filter(Q(source_prompt=prompt) | Q(prompt=prompt.text))
            .exclude(response_text="")
            .values_list("provider", "model_id")
        )
        for answered_provider, answered_model in answered_rows:
            already_answered.add(answered_provider)
            if answered_model:
                already_answered.add(f"{answered_provider}:{answered_model}")

    queried_providers: list[str] = []
    skipped_have_answer = 0

    # The models this prompt runs against: the BrandPrompt's explicit
    # selection when one exists (several variants of one provider are
    # allowed), else every configured provider's default model — the
    # pre-selection behaviour, unchanged.
    selected = list(
        BrandPrompt.objects
        .filter(website=website, prompt=prompt)
        .values_list("model_variants", flat=True)
        .first() or []
    )

    # (gap-fill skip key, provider key, bound provider instance)
    run_specs: list[tuple] = []
    if selected:
        seen_variants: set[str] = set()
        for vid in selected:
            vid = str(vid or "").strip()
            if not vid or vid in seen_variants or parse_variant(vid) is None:
                continue
            seen_variants.add(vid)
            if only is not None and vid.split(":", 1)[0] not in only:
                continue
            if vid in already_answered:
                skipped_have_answer += 1
                continue
            instance = get_provider_for_variant(vid)
            if instance is None:
                errors.append(f"{vid}: provider not configured")
                continue
            run_specs.append((vid, vid.split(":", 1)[0], instance))
        if not run_specs and not skipped_have_answer and not errors:
            errors.append("no configured models in this prompt's selection")
    else:
        try:
            variants = list_model_variants()
        except Exception as exc:
            variants = []
            errors.append(f"model registry unavailable: {exc!s}"[:200])
        if not variants and not errors:
            errors.append("model registry returned no variants")

        seen_providers: set[str] = set()
        for v in variants:
            # list_model_variants() returns dicts. The previous attribute-style
            # reads (getattr) were always falsy on dicts, which silently skipped
            # EVERY provider — scans "ran" without querying a single model.
            if isinstance(v, dict):
                configured = v.get("configured", False)
                provider_key = v.get("provider", "")
            else:  # defensive: tolerate object-shaped variants too
                configured = getattr(v, "configured", False)
                provider_key = getattr(v, "provider", "")
            if not configured:
                continue
            if not provider_key or provider_key in seen_providers:
                continue
            seen_providers.add(provider_key)
            if only is not None and provider_key not in only:
                continue
            if provider_key in already_answered:
                skipped_have_answer += 1
                continue
            cls = PROVIDERS.get(provider_key)
            if cls is None:
                continue
            try:
                instance = cls()
            except Exception as exc:
                errors.append(f"{provider_key}: {exc!s}")
                continue
            run_specs.append((provider_key, provider_key, instance))

    # Run the original prompt against each configured provider and
    # log the responses as LLMRankingResult rows on a synthetic audit.
    # The Prompt-detail page reads from LLMRankingResult, so this is
    # what makes the visibility/sentiment numbers light up. No audit is
    # created when there is nothing to ask.
    audit = None
    if run_specs and stop_requested():
        stopped, run_specs = True, []
    if run_specs:
        audit = LLMRankingAudit.objects.create(
            website=website,
            # Audit rows require a created_by; the crawl is run on behalf of
            # the website's owner regardless of which user kicked the scan.
            created_by=website.user,
            business_name=brand_name,
            status=LLMRankingAudit.STATUS_RUNNING,
            prompt_source=getattr(LLMRankingAudit, "PROMPT_SOURCE_LIBRARY", "library"),
            prompts=[prompt.text],
            providers_queried=[],
            started_at=timezone.now(),
        )

    for _skip_key, provider_key, instance in run_specs:
        if stop_requested():
            stopped = True
            break
        # One query, one retry. A model that doesn't answer (transient
        # error, rate limit) gets a single second attempt, then we record
        # the failure and move on instead of looping. A hard failure with
        # no key returns succeeded=False immediately and isn't retried.
        result = _query_with_retry(
            instance, prompt.text,
            user=spend_user, website=website, audit_id=str(audit.id), actor=actor,
            run_feature=run_feature, run_key=f"crawl:{run.id}:{_skip_key}",
        )
        if result is None:
            errors.append(f"{provider_key}: query raised on both attempts")
            continue

        # A provider that returns succeeded=False (missing key, rate limit,
        # circuit open) still gets a row so the detail page can show the
        # state, but we skip extraction on it.
        succeeded = getattr(result, "succeeded", True)
        response_text = getattr(result, "text", None) or ""
        err = getattr(result, "error", "") or ""

        analysis = _extract_brands(
            response_text=response_text if succeeded else "",
            website=website,
            brand_name=brand_name,
            keywords=keywords,
            audit_id=str(audit.id),
            user=spend_user,
        )

        # The exact model that answered — distinguishes rows when a prompt
        # scans several variants of one provider. Guarded to str: provider
        # doubles in tests carry mock attributes.
        model_used = getattr(instance, "model", "")
        if not isinstance(model_used, str):
            model_used = ""

        result_obj = LLMRankingResult.objects.create(
            audit=audit,
            provider=provider_key,
            model_id=model_used[:64],
            prompt_index=0,
            prompt=prompt.text,
            source_prompt=prompt,
            response_text=response_text,
            query_succeeded=succeeded,
            error_message=err[:500],
            is_mentioned=analysis["is_mentioned"],
            mention_rank=analysis["mention_rank"],
            sentiment=analysis["sentiment"],
            confidence_score=analysis["confidence_score"],
            mention_context=analysis["mention_context"],
            is_linked=analysis.get("is_linked", False),
            competitors_mentioned=analysis.get("competitors_mentioned", []),
            primary_recommendation=analysis.get("primary_recommendation", ""),
            # Provider-reported sources first (Perplexity's citation list,
            # Claude's web-search citations) — this path used to store only
            # the analysis-extracted URLs, silently dropping every native
            # source on per-prompt scans while full audits kept them.
            citations=_merge_citation_lists(
                getattr(result, "citations", None), analysis.get("citations"),
            ),
            extraction_model=analysis.get("extraction_model", ""),
            extraction_version=analysis.get("extraction_version", ""),
            **search_query_fields(result),
            **citation_passage_fields(result, provider_key),
        )
        if succeeded:
            responses_logged += 1
            _dispatch_citations(result_obj.id)
            _dispatch_alignment(result_obj.id)
        else:
            errors.append(f"{provider_key}: {err[:120]}")
        queried_providers.append(provider_key)

    # "Have what we need" = we logged a response this run OR every model
    # was already answered on a previous run. Either way the crawl is a
    # success, not a failure.
    have_coverage = bool(responses_logged or skipped_have_answer)

    if audit is not None:
        # The audit itself is scored only from the answers it holds.
        scores = LLMRankingService.compute_overall_score(list(audit.results.all()))
        for field, value in scores.items():
            setattr(audit, field, value)
        audit.providers_queried = queried_providers
        audit.status = (
            LLMRankingAudit.STATUS_COMPLETED if responses_logged
            else LLMRankingAudit.STATUS_FAILED
        )
        audit.completed_at = timezone.now()
        audit.save(update_fields=[
            *SCORE_FIELDS, "providers_queried", "status", "completed_at",
        ])

    # Brand Security reads the responses this crawl just stored. Async
    # because the auditor may escalate findings to the LLM judge, and that
    # latency does not belong inside a crawl. Full-audit completions get
    # the same treatment inline in ranking_service; this closes the gap
    # for per-prompt crawls and their 15-minute scheduled scans.
    if audit is not None and audit.status == LLMRankingAudit.STATUS_COMPLETED:
        try:
            from apps.brand_vault.tasks import audit_recent_results
            audit_recent_results.delay(str(website.id), str(audit.id))
        except Exception as exc:
            logger.warning(
                "could not queue brand security audit for crawl %s: %s",
                run.id, exc,
            )

    run.providers = queried_providers
    run.fanout_count = len(fanouts)
    run.source_count = responses_logged
    run.status = (
        PromptCrawlRun.STATUS_COMPLETE if have_coverage or fanouts
        else PromptCrawlRun.STATUS_FAILED
    )
    # A failed run must always say WHY: an empty error rendered as
    # "unknown error" in the UI and left nothing to debug with. The only
    # silent path here is the fan-out LLM call (it swallows its own
    # exception and returns []) with no providers left to query.
    if run.status == PromptCrawlRun.STATUS_FAILED and not errors and not stopped:
        errors.append(
            "fan-out generated no sub-queries and no model was queried "
            "(fan-out LLM call likely failed; check provider keys and logs)"
        )
    run_errors = [*errors, "stopped before every model was asked"] if stopped else errors
    run.error = "; ".join(run_errors)[:1000]
    run.completed_at = timezone.now()
    run.save(update_fields=[
        "providers", "fanout_count", "source_count",
        "status", "error", "completed_at",
    ])

    return CrawlOutcome(fanouts=fanouts, responses=responses_logged, errors=errors,
                        stopped=stopped)


# ── One engine at a time (the agent's research swarm) ──────────────────


@dataclass
class EngineAnswer:
    """One engine's answer to one prompt, as EngineScan.ask stored it."""

    succeeded: bool
    # Raw provider text: for logs and public_engine_error only.
    error: str = ""
    response_text: str = ""
    # _extract_brands output (is_mentioned, mention_rank, sentiment,
    # competitors_mentioned, mention_context, ...).
    analysis: dict = field(default_factory=dict)
    # The merged citation URLs stored on the result row.
    citations: list = field(default_factory=list)
    model_id: str = ""
    # LLMRankingResult.public_id of the stored row.
    result_id: str | None = None
    created_at: datetime | None = None
    # Not asked: Stop came first, or the scan had finished.
    stopped: bool = False
    # Not asked: the engine has no API key.
    not_configured: bool = False


class EngineScan:
    """One prompt's AI engine checks, asked one engine at a time (the agent's
    research swarm). Records what crawl_prompt records for the same engines:
    one PromptCrawlRun, one LLMRankingAudit and one LLMRankingResult per engine
    asked, citation and alignment tasks per answer, and the brand security
    audit at the end. It does not generate fan-out sub-queries.

    ``ask`` is safe to call from several threads at once (one per engine);
    the crawl run and the audit are created by the first call that asks an
    engine. ``finish`` scores the audit and closes the crawl run, once. An
    answer that arrives after ``finish`` (a thread still waiting on its
    engine after Stop) is stored, but the audit is not scored again.

    Spend goes to ``acting_user``, tagged ``actor`` ("user" for the agent,
    "system" for Autopilot's daily run) and ``usage_tags`` on each answer's
    usage row. Each answer is one prompt run of ``feature`` on the project
    owner's account; with none left the engine is not asked.
    """

    def __init__(self, website: Website, prompt: Prompt, *, acting_user, actor: str = "user",
                 usage_tags: dict | None = None, feature: str = "agent_research"):
        self.website = website
        self.prompt = prompt
        self.acting_user = acting_user
        self.actor = actor
        # The prompt-run feature each answer is counted under.
        self.feature = feature
        self.usage_tags = dict(usage_tags or {})
        self.brand_name = getattr(website, "business_name", None) or website.name or "your brand"
        self.keywords = _website_keywords(website)
        self._lock = threading.Lock()
        self._crawl_run: PromptCrawlRun | None = None
        self._audit: LLMRankingAudit | None = None
        self._queried: list[str] = []
        self._errors: list[str] = []
        self._responses = 0
        self._finished = False
        self._outcome: CrawlOutcome | None = None

    @property
    def audit_id(self) -> str | None:
        """The scan's audit id once an engine has been asked, else None."""
        with self._lock:
            audit = self._audit
        return str(audit.id) if audit is not None else None

    def _rows(self) -> LLMRankingAudit | None:
        """The audit to store answers on (with its crawl run), created on
        first use; None once the scan finished."""
        with self._lock:
            if self._finished:
                return None
            if self._audit is None:
                now = timezone.now()
                self._crawl_run = PromptCrawlRun.objects.create(
                    website=self.website,
                    prompt=self.prompt,
                    status=PromptCrawlRun.STATUS_RUNNING,
                    started_at=now,
                )
                self._audit = LLMRankingAudit.objects.create(
                    website=self.website,
                    # As in crawl_prompt: audits run on behalf of the owner.
                    created_by=self.website.user,
                    business_name=self.brand_name,
                    status=LLMRankingAudit.STATUS_RUNNING,
                    prompt_source=getattr(LLMRankingAudit, "PROMPT_SOURCE_LIBRARY", "library"),
                    prompts=[self.prompt.text],
                    providers_queried=[],
                    started_at=now,
                )
            return self._audit

    def _note_error(self, line: str) -> None:
        with self._lock:
            if not self._finished:
                self._errors.append(line)

    def ask(self, provider_key: str, variant: str | None, *,
            should_stop: Callable[[], bool] | None = None,
            on_step: Callable[[str], None] | None = None) -> EngineAnswer:
        """Ask one engine (the provider's default model, or ``variant``,
        "provider:model_id") and store its answer as crawl_prompt does.
        ``on_step("extracting")`` is called once the answer arrived."""
        try:
            instance = (get_provider_for_variant(variant) if variant
                        else PROVIDERS[provider_key]())
        except Exception as exc:
            self._note_error(f"{provider_key}: {exc!s}"[:200])
            return EngineAnswer(succeeded=False, error=str(exc)[:300])
        if instance is None or not instance.is_configured():
            self._note_error(f"{variant or provider_key}: provider not configured")
            return EngineAnswer(succeeded=False, not_configured=True,
                                error=f"{provider_key}: provider not configured")
        if should_stop is not None and should_stop():
            return EngineAnswer(succeeded=False, stopped=True)
        audit = self._rows()
        if audit is None:
            return EngineAnswer(succeeded=False, stopped=True)

        model_used = getattr(instance, "model", "")
        if not isinstance(model_used, str):
            model_used = ""
        crawl_run = self._crawl_run
        result = _query_with_retry(
            instance, self.prompt.text,
            user=self.acting_user, website=self.website, audit_id=str(audit.id),
            actor=self.actor, extra_metadata=self.usage_tags or None,
            run_feature=self.feature,
            run_key=f"scan:{crawl_run.id}:{variant or provider_key}" if crawl_run else None,
        )
        if result is None:
            self._note_error(f"{provider_key}: query raised on both attempts")
            return EngineAnswer(succeeded=False, error="query raised on both attempts",
                                model_id=model_used[:64])
        if on_step is not None:
            on_step("extracting")

        succeeded = getattr(result, "succeeded", True)
        response_text = getattr(result, "text", None) or ""
        err = getattr(result, "error", "") or ""
        analysis = _extract_brands(
            response_text=response_text if succeeded else "",
            website=self.website,
            brand_name=self.brand_name,
            keywords=self.keywords,
            audit_id=str(audit.id),
            user=self.acting_user,
        )
        citations = _merge_citation_lists(getattr(result, "citations", None),
                                          analysis.get("citations"))
        row = LLMRankingResult.objects.create(
            audit=audit,
            provider=provider_key,
            model_id=model_used[:64],
            prompt_index=0,
            prompt=self.prompt.text,
            source_prompt=self.prompt,
            response_text=response_text,
            query_succeeded=succeeded,
            error_message=err[:500],
            is_mentioned=analysis["is_mentioned"],
            mention_rank=analysis["mention_rank"],
            sentiment=analysis["sentiment"],
            confidence_score=analysis["confidence_score"],
            mention_context=analysis["mention_context"],
            is_linked=analysis.get("is_linked", False),
            competitors_mentioned=analysis.get("competitors_mentioned", []),
            primary_recommendation=analysis.get("primary_recommendation", ""),
            citations=citations,
            extraction_model=analysis.get("extraction_model", ""),
            extraction_version=analysis.get("extraction_version", ""),
            **search_query_fields(result),
            **citation_passage_fields(result, provider_key),
        )
        with self._lock:
            if not self._finished:
                self._queried.append(provider_key)
                if succeeded:
                    self._responses += 1
                else:
                    self._errors.append(f"{provider_key}: {err[:120]}")
        if succeeded:
            _dispatch_citations(row.id)
            _dispatch_alignment(row.id)
        return EngineAnswer(
            succeeded=succeeded,
            error=err,
            response_text=response_text,
            analysis=analysis,
            citations=citations,
            model_id=model_used[:64],
            result_id=str(row.public_id),
            created_at=row.created_at,
        )

    def finish(self, *, stopped: bool = False) -> CrawlOutcome:
        """Score the audit, queue the brand security audit and close the
        crawl run, as crawl_prompt ends. Calling it again changes nothing."""
        with self._lock:
            if self._finished and self._outcome is not None:
                return self._outcome
            self._finished = True
            crawl_run, audit = self._crawl_run, self._audit
            queried, errors, responses = list(self._queried), list(self._errors), self._responses

        if audit is not None:
            scores = LLMRankingService.compute_overall_score(list(audit.results.all()))
            for name, value in scores.items():
                setattr(audit, name, value)
            audit.providers_queried = queried
            audit.status = (
                LLMRankingAudit.STATUS_COMPLETED if responses
                else LLMRankingAudit.STATUS_FAILED
            )
            audit.completed_at = timezone.now()
            audit.save(update_fields=[
                *SCORE_FIELDS, "providers_queried", "status", "completed_at",
            ])
            if audit.status == LLMRankingAudit.STATUS_COMPLETED:
                try:
                    from apps.brand_vault.tasks import audit_recent_results
                    audit_recent_results.delay(str(self.website.id), str(audit.id))
                except Exception as exc:
                    logger.warning(
                        "could not queue brand security audit for crawl %s: %s",
                        crawl_run.id if crawl_run else None, exc,
                    )

        if crawl_run is not None:
            crawl_run.providers = queried
            crawl_run.fanout_count = 0
            crawl_run.source_count = responses
            crawl_run.status = (
                PromptCrawlRun.STATUS_COMPLETE if responses else PromptCrawlRun.STATUS_FAILED
            )
            run_errors = [*errors, "stopped before every model was asked"] if stopped else errors
            if crawl_run.status == PromptCrawlRun.STATUS_FAILED and not run_errors:
                run_errors = ["no engine answered"]
            crawl_run.error = "; ".join(run_errors)[:1000]
            crawl_run.completed_at = timezone.now()
            crawl_run.save(update_fields=[
                "providers", "fanout_count", "source_count",
                "status", "error", "completed_at",
            ])

        outcome = CrawlOutcome(fanouts=[], responses=responses, errors=errors, stopped=stopped)
        with self._lock:
            self._outcome = outcome
        return outcome
