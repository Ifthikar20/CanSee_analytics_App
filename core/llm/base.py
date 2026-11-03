"""
Base contract for every LLM call in the project.

Subclasses implement only `_call()` (the SDK quirks). The base handles:
  - duration measurement
  - centralized token/cost recording via core.ai_tracking.record_usage
  - the plan-derived monthly spend wall (fail-closed for attributed users)
  - error envelope normalisation
  - per-provider rate limiting (Redis token bucket)
  - per-provider circuit breaker (Redis-backed, fails open on cache outage)
so adding a new provider or call site can never silently skip cost
tracking again.

Lives in core (not an app) because it depends only on core.resilience and
core.ai_tracking: any app may import it without creating cross-app edges.
The audit-answer providers in apps/llm_ranking/providers subclass this;
utility calls (extraction, drafting, onboarding) use core.llm.utility.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from core.resilience import (
    CircuitBreaker,
    TokenBucket,
)

logger = logging.getLogger("core.llm")

# Per-provider defaults. Conservative — most providers' real limits are higher,
# but we want to smooth bursts at the audit-fan-out point and stay well under
# any free-tier or trial-tier ceiling. Override per provider class as needed.
_DEFAULT_RPM = 60          # 60 requests / minute = 1 RPS steady state
_DEFAULT_BURST = 20        # burst capacity (queue smoothing)
_DEFAULT_FAILURE_THRESHOLD = 5
_DEFAULT_RECOVERY_SECONDS = 60


def allowance_denial(user) -> str | None:
    """Monthly spend-wall check, shared by the chat providers below and
    non-chat spenders (embeddings). Returns a denial reason string, or
    None when the call may proceed. Fail-closed: if the allowance lookup
    itself breaks for an attributed user we deny rather than risk
    unbounded spend. Unattributed calls (user=None, e.g. health checks)
    are exempt.
    """
    if user is None:
        return None
    try:
        from core.ai_tracking import effective_ai_cap, month_to_date_cost

        cap = effective_ai_cap(user)
        if cap > 0 and month_to_date_cost(user) >= cap:
            return (
                "monthly_ai_allowance_exceeded: the account has "
                f"used its ${cap:.2f} monthly AI allowance"
            )
    except Exception:
        logger.warning(
            "AI allowance check failed; denying call to stay fail-closed",
            exc_info=True,
        )
        return "ai_allowance_check_failed: could not verify AI allowance"
    return None


@dataclass
class ProviderResult:
    """Uniform envelope returned by every LLMProvider.query() call."""

    succeeded: bool
    text: str = ""
    error: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    raw: dict = field(default_factory=dict)
    # Source URLs the provider itself reported (web-grounded models like
    # Perplexity return these out-of-band; the answer text only carries
    # [1][2] markers, so text extraction alone can never recover them).
    citations: list = field(default_factory=list)
    # Server-side web searches the upstream ran and bills per search
    # (Anthropic's web_search tool); priced into the recorded cost.
    web_search_requests: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class LLMProvider:
    """Abstract base. Subclasses set name/model/api_key_setting and implement _call()."""

    # Audit-side key used in LLMRankingResult.provider and PROVIDERS registry.
    name: str = ""
    # Concrete model identifier sent to the SDK and stored in AITokenUsage.
    model: str = ""
    # Django settings attribute holding the API key for this provider.
    api_key_setting: str = ""
    # Default per-call timeout. Subclasses may override.
    timeout_seconds: int = 30

    # Rate limiting & breaker — overridable per-provider class.
    rpm: int = _DEFAULT_RPM
    burst: int = _DEFAULT_BURST
    failure_threshold: int = _DEFAULT_FAILURE_THRESHOLD
    recovery_seconds: int = _DEFAULT_RECOVERY_SECONDS

    def __init__(self):
        from django.conf import settings
        self.api_key = getattr(settings, self.api_key_setting, "") if self.api_key_setting else ""

    def is_configured(self) -> bool:
        """True when the API key is present and the SDK can be imported."""
        return bool(self.api_key)

    def _bucket(self) -> TokenBucket:
        # Refill rate is per-second so a 60 RPM provider drips at 1 token/sec
        # and bursts up to `burst`.
        return TokenBucket(
            name=f"llm:{self.name}",
            capacity=self.burst,
            refill_per_second=self.rpm / 60.0,
        )

    def _breaker(self) -> CircuitBreaker:
        return CircuitBreaker(
            name=f"llm:{self.name}",
            failure_threshold=self.failure_threshold,
            recovery_timeout=self.recovery_seconds,
        )

    def query(
        self,
        prompt: str,
        system_prompt: str = "",
        *,
        user=None,
        website=None,
        audit_id: str | None = None,
        role: str = "upstream",
        region: str = "",
        module: str = "llm_ranking",
        idempotency_key: str | None = None,
        extra_metadata: dict | None = None,
    ) -> ProviderResult:
        """
        Run a single prompt against the provider.

        Records token usage centrally for every call the upstream billed
        (a failed result that reports tokens included), attributed to
        ``module`` (the AITokenUsage module key — pass the calling app's
        name so the per-module spend breakdown stays honest). Retryable
        call sites (Celery acks_late tasks) pass ``idempotency_key`` so a
        redelivered task cannot double-count the same logical call;
        ``extra_metadata`` adds flat tags to the usage row (e.g.
        actor="system" for scheduled work). On failure returns a ProviderResult with
        succeeded=False and the upstream error string — callers should
        check `.succeeded` rather than catching exceptions.
        """
        if not self.is_configured():
            return ProviderResult(
                succeeded=False,
                error=f"service_unavailable: {self.name} provider not enabled",
            )

        # Hard spend wall. Views pre-check the allowance for friendly
        # errors, but this is the enforcement point: every provider call
        # for an attributed user passes through here, so no feature —
        # current or future — can spend past the plan-derived cap by more
        # than the one call already in flight.
        denial = allowance_denial(user)
        if denial:
            return ProviderResult(succeeded=False, error=denial)

        # Per-provider circuit breaker. When OPEN we short-circuit without
        # consuming a rate-limit token or hitting the upstream API. The
        # breaker fails open on cache outage so a Redis blip can't stop work.
        breaker = self._breaker()
        if not breaker.allow():
            return ProviderResult(
                succeeded=False,
                error=f"{self.name} circuit open (recent failures); skipping",
            )

        # Per-provider token bucket. Smooths burst at audit fan-out time and
        # keeps us under upstream tier limits. Fails open on cache outage.
        if not self._bucket().try_acquire():
            return ProviderResult(
                succeeded=False,
                error=f"{self.name} rate limit exceeded; try again shortly",
            )

        t0 = time.monotonic()
        try:
            result = self._call(
                prompt=prompt, system_prompt=system_prompt, region=region,
            )
        except Exception as exc:  # noqa: BLE001 — providers throw a wide variety
            duration_ms = int((time.monotonic() - t0) * 1000)
            logger.warning("Provider %s call failed: %s", self.name, exc)
            breaker.record_failure()
            # Truncate to keep API keys / large stack traces out of the audit
            # log just in case the SDK ever surfaces them.
            return ProviderResult(
                succeeded=False, error=str(exc)[:300], duration_ms=duration_ms,
            )

        result.duration_ms = int((time.monotonic() - t0) * 1000)

        # Tokens on a result mean the upstream answered and billed the call,
        # even when the answer is unusable (a refusal, an answer cut off at
        # max_tokens): the service is healthy, and the spend is real.
        billed = bool(result.input_tokens or result.output_tokens)
        if result.succeeded or billed:
            breaker.record_success()
        else:
            breaker.record_failure()
        if billed:
            self._record(
                result=result, user=user, website=website,
                audit_id=audit_id, role=role, module=module,
                idempotency_key=idempotency_key,
                extra_metadata=extra_metadata,
            )
        return result

    # ── To implement in subclasses ────────────────────────────────────────

    def _call(self, *, prompt: str, system_prompt: str,
              region: str = "") -> ProviderResult:
        raise NotImplementedError

    # ── Centralised cost recording ────────────────────────────────────────

    def _record(self, *, result, user, website, audit_id, role,
                module: str = "llm_ranking",
                idempotency_key: str | None = None,
                extra_metadata: dict | None = None):
        try:
            from core.ai_tracking import record_usage
            metadata = {"role": role}
            if audit_id:
                metadata["audit_id"] = str(audit_id)
            if extra_metadata:
                metadata.update(extra_metadata)
            record_usage(
                module=module,
                model_name=self.model,
                provider=self._provider_label(),
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                user=user,
                website=website,
                duration_ms=result.duration_ms,
                metadata=metadata,
                idempotency_key=idempotency_key,
                web_search_requests=getattr(result, "web_search_requests", 0),
            )
        except Exception as exc:  # never let bookkeeping break a query
            logger.warning("record_usage failed for %s: %s", self.name, exc)

    def _provider_label(self) -> str:
        """Map audit-side key to AITokenUsage.provider value."""
        return {
            "claude": "anthropic",
            "gpt4": "openai",
            "gemini": "google",
            "perplexity": "perplexity",
            "deepseek": "deepseek",
            "grok": "xai",
        }.get(self.name, self.name)
