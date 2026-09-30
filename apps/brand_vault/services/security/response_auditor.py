"""Raise Brand Security findings from audit responses already on record.

The external-source security agents each re-query providers with their own
``SafetyPrompt``. That costs a second round of API calls and, more
importantly, judges answers nobody ever sees: the responses on the
dashboard and the prompt detail page are a different set entirely.

This module reads ``LLMRankingResult`` rows the audits already produced
and raises ``SafetyAlert`` rows linked back to the exact response via
``SafetyAlert.result``. Every finding is therefore traceable — a reader
can open the answer the judgement was made against, and the alert's
``evidence_spans`` mark the exact characters that triggered it.

Detection is the registry in :mod:`.detectors` — one stable code per
trigger type (``BS-SENT-001`` ...). Nuanced detectors escalate to the
RAG-grounded LLM judge according to their ``judge_mode``, behind three
gates: the ``BRAND_SECURITY_JUDGE_ENABLED`` setting, the per-website
``BrandSecurityConfig.llm_judge_enabled`` flag, and the tenant's monthly
AI cost cap (plus a hard per-scan call budget).

Persistence contract:

* Idempotent per ``(website, result, detector_code)`` — rescanning the
  same response changes nothing.
* A finding recurring on a *new* response of the same prompt/provider
  stream (same ``dedupe_key``) bumps ``occurrence_count`` on the open
  alert and repoints it at the newest response instead of stacking a
  duplicate row per scan.
* Resolved and dismissed alerts are never reopened — a re-firing after
  resolution creates a fresh alert with a fresh reference.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from .detectors import (
    DetectedFinding,
    DetectionContext,
    compute_dedupe_key,
    run_detectors,
)

logger = logging.getLogger("apps")

AGENT_ID = "response_auditor"
DISPLAY_NAME = "Response Auditor"
DESCRIPTION = (
    "Reads every stored LLM answer from your audits, prompt runs and "
    "chat checks, and raises findings with the exact phrases that "
    "triggered them."
)

# Hard per-scan budget for judge escalations, independent of the tenant's
# monthly cost cap. One scan can touch hundreds of responses; the judge
# should sharpen the worst findings, not adjudicate all of them.
MAX_JUDGE_CALLS_PER_SCAN = 10

_GROUND_TRUTH_TOP_K = 5
_JUDGEABLE_SEVERITIES = ("high", "medium", "low")
_FACT_ISSUES = ("hallucination", "unverified", "outdated")
# Verdicts that mean "the model asserted something false": high severity
# whenever the judge does not grade explicitly.
_HIGH_ON_CONFIRM = ("hallucination", "hallucinated_compliance", "false_incident")


class _JudgeGate:
    """Per-scan budget + kill switches for LLM judge escalation."""

    def __init__(self, website):
        self.calls_left = MAX_JUDGE_CALLS_PER_SCAN
        self.enabled = bool(getattr(settings, "BRAND_SECURITY_JUDGE_ENABLED", True))
        if self.enabled and not (getattr(settings, "ANTHROPIC_API_KEY", "") or ""):
            self.enabled = False
        if self.enabled:
            try:
                from apps.brand_vault.models import BrandSecurityConfig
                cfg = BrandSecurityConfig.objects.filter(website=website).first()
                if cfg and not cfg.llm_judge_enabled:
                    self.enabled = False
            except Exception:  # pragma: no cover — config read never blocks a scan
                pass
        if self.enabled:
            try:
                from core.ai_tracking import effective_ai_cap, month_to_date_cost
                cap = effective_ai_cap(website.user)
                if cap > 0 and month_to_date_cost(website.user) >= cap:
                    self.enabled = False
                    logger.info(
                        "brand security judge disabled for %s: AI cost cap reached",
                        website.id,
                    )
            except Exception:  # pragma: no cover
                pass

    def take(self) -> bool:
        if not self.enabled or self.calls_left <= 0:
            return False
        self.calls_left -= 1
        return True


def _retrieve_ground_truth(website, query: str) -> list[dict]:
    """The client's own statements relevant to ``query``, as chunk dicts."""
    try:
        from apps.rag.services.retriever import retrieve
        hits = retrieve(
            user=website.user, website=website,
            query=(query or "")[:300], top_k=_GROUND_TRUTH_TOP_K,
        )
    except Exception:
        # Retrieval must never break the scan — the finding is simply
        # skipped (fact checks are meaningless without ground truth).
        return []
    return [
        {
            "chunk_id": h.chunk_id,
            "source_id": h.source_id,
            "source_url": h.source_url,
            "section_label": h.section_label,
            "text": h.text,
            "score": h.score,
        }
        for h in hits
    ]


def _escalate(
    finding: DetectedFinding, gate: _JudgeGate, website, brand: str,
) -> tuple[bool, list[dict]]:
    """Apply the detector's judge_mode. Returns ``(keep, evidence_chunks)``.

    Mutates ``finding`` in place when the judge re-grades it.
    """
    from .judge import judge_finding

    mode = finding.detector.judge_mode
    if mode == "never":
        return True, []

    if mode == "confirm":
        if not gate.take():
            return True, []  # heuristic verdict stands at default severity
        verdict = judge_finding(
            question=finding.detector.judge_question,
            brand=brand,
            title=finding.title,
            snippet=finding.snippet,
            allowed_issues=(finding.issue,),
            user=website.user,
            website=website,
        )
        if verdict.issue == "none":
            # A real verdict carries a detail sentence; an empty one is the
            # no-op returned on a failed call — keep the heuristic then.
            return (not verdict.detail), []
        if verdict.severity in _JUDGEABLE_SEVERITIES:
            finding.severity = verdict.severity
        if verdict.sentiment_score is not None:
            finding.sentiment_score = max(-1.0, min(1.0, float(verdict.sentiment_score)))
        if verdict.detail:
            finding.detail = f"{finding.detail} Judge: {verdict.detail}"
        return True, []

    # mode == "require": the finding only exists on a grounded confirmation.
    ground_truth = _retrieve_ground_truth(website, finding.snippet)
    if not ground_truth:
        return False, []
    if not gate.take():
        return False, []
    allowed_issues = tuple(finding.detector.judge_issues) or _FACT_ISSUES
    verdict = judge_finding(
        question=finding.detector.judge_question,
        brand=brand,
        title=finding.title,
        snippet=finding.snippet,
        allowed_issues=allowed_issues,
        ground_truth=ground_truth,
        user=website.user,
        website=website,
    )
    if verdict.issue not in allowed_issues:
        return False, []
    finding.issue = verdict.issue
    if verdict.severity in _JUDGEABLE_SEVERITIES:
        finding.severity = verdict.severity
    elif verdict.issue in _HIGH_ON_CONFIRM:
        finding.severity = "high"
    if verdict.sentiment_score is not None:
        finding.sentiment_score = max(-1.0, min(1.0, float(verdict.sentiment_score)))
    if verdict.detail:
        finding.detail = verdict.detail
    return True, ground_truth


def _persist_finding(website, result, finding: DetectedFinding, evidence_chunks: list[dict]):
    """Write one finding. Returns ``(alert | None, created: bool)``."""
    from apps.brand_vault.models import SafetyAlert, generate_alert_reference

    detector = finding.detector
    dedupe = compute_dedupe_key(detector.code, result)
    span_dicts = [s.as_dict() for s in finding.spans]

    # 1) This exact response was already scanned by this detector: no-op,
    #    no occurrence bump — audit hooks and the catch-up beat overlap.
    existing = SafetyAlert.objects.filter(
        website=website, result=result, detector_code=detector.code,
    ).first()
    if existing:
        return existing, False

    # 2) Same finding recurring on a new response of the same
    #    prompt/provider stream: one alert, occurrence bumped, repointed
    #    at the newest response so "view conversation" shows current state.
    recurring = (
        SafetyAlert.objects
        .filter(website=website, dedupe_key=dedupe, status=SafetyAlert.STATUS_OPEN)
        .order_by("-last_seen_at")
        .first()
    )
    if recurring:
        current = recurring.result
        if current is not None and result.created_at <= current.created_at:
            # The alert already reflects a response at least as new as this
            # one (a scan window can cover several responses of the same
            # stream) — bumping here would double-count and repoint the
            # alert backwards.
            return recurring, False
        recurring.occurrence_count += 1
        recurring.last_seen_at = timezone.now()
        recurring.result = result
        recurring.model = result.provider or ""
        recurring.prompt_text = (result.prompt or "")[:500]
        recurring.title = finding.title[:300]
        recurring.detail = finding.detail
        recurring.snippet = finding.snippet
        recurring.severity = finding.severity
        recurring.issue = finding.issue
        recurring.sentiment_score = finding.sentiment_score
        recurring.evidence_spans = span_dicts
        if evidence_chunks:
            recurring.evidence_chunks = evidence_chunks
        try:
            with transaction.atomic():
                recurring.save(update_fields=[
                    "occurrence_count", "last_seen_at", "result", "model",
                    "prompt_text", "title", "detail", "snippet", "severity",
                    "issue", "sentiment_score", "evidence_spans",
                    "evidence_chunks", "updated_at",
                ])
        except IntegrityError:
            # A concurrent scan already attached an alert to this response.
            return None, False
        return recurring, False

    # 3) Fresh alert. Resolved/dismissed history stays closed — a
    #    re-firing after resolution is a new event with a new reference.
    now = timezone.now()
    fields = dict(
        website=website,
        result=result,
        detector_code=detector.code,
        dedupe_key=dedupe,
        first_seen_at=now,
        last_seen_at=now,
        agent_id=AGENT_ID,
        source=SafetyAlert.SOURCE_LLM,
        model=result.provider or "",
        prompt_text=(result.prompt or "")[:500],
        title=finding.title[:300],
        detail=finding.detail,
        snippet=finding.snippet,
        issue=finding.issue,
        severity=finding.severity,
        sentiment_score=finding.sentiment_score,
        evidence_spans=span_dicts,
        evidence_chunks=evidence_chunks or [],
    )
    try:
        with transaction.atomic():
            return SafetyAlert.objects.create(**fields), True
    except IntegrityError:
        concurrent = SafetyAlert.objects.filter(
            website=website, result=result, detector_code=detector.code,
        ).first()
        if concurrent:
            return concurrent, False
        # Astronomically rare reference collision — retry once explicitly.
        try:
            with transaction.atomic():
                fields["reference"] = generate_alert_reference()
                return SafetyAlert.objects.create(**fields), True
        except IntegrityError:  # pragma: no cover
            logger.warning(
                "brand security alert write failed twice for result %s / %s",
                result.pk, detector.code,
            )
            return None, False


def audit_results_for_website(website, *, audit=None, limit: int = 400, since=None) -> int:
    """Scan stored audit responses and raise linked SafetyAlerts.

    ``audit`` narrows the scan to one audit's responses; ``since`` (a
    datetime) narrows it to responses created after that moment — the
    catch-up beat task's watermark. Idempotent throughout: re-running
    creates no duplicates and bumps no occurrence counts.

    Returns the number of alerts created.
    """
    from apps.brand_vault.services.security.agents._helpers import brand_terms
    from apps.llm_ranking.models import LLMRankingAudit, LLMRankingResult

    audits = LLMRankingAudit.objects.filter(
        website=website, status=LLMRankingAudit.STATUS_COMPLETED,
    )
    if audit is not None:
        audits = audits.filter(id=audit.id)
    audit_ids = list(audits.values_list("id", flat=True))
    if not audit_ids:
        return 0

    brand = ""
    latest = audits.order_by("-completed_at").first()
    if latest:
        brand = (latest.business_name or "").strip()
    if not brand:
        brand = (getattr(website, "name", "") or "").strip()
    if not brand:
        return 0

    terms = [brand] + [
        t for t in brand_terms(website, {})
        if t and t.lower() != brand.lower()
    ]

    results = (
        LLMRankingResult.objects
        .filter(audit_id__in=audit_ids, query_succeeded=True)
    )
    if since is not None:
        results = results.filter(created_at__gte=since)
    results = results.order_by("-created_at")[:limit]

    gate = _JudgeGate(website)
    created = 0
    new_alerts = []
    for result in results:
        competitors = [
            c for c in (result.competitors_mentioned or [])
            if isinstance(c, dict) and (c.get("name") or "").strip()
        ]
        ctx = DetectionContext(
            result=result,
            text=result.response_text or "",
            brand=brand,
            brand_terms=terms,
            competitors=competitors,
        )
        for finding in run_detectors(ctx):
            keep, evidence_chunks = _escalate(finding, gate, website, brand)
            if not keep:
                continue
            alert, made = _persist_finding(website, result, finding, evidence_chunks)
            if made:
                created += 1
                new_alerts.append(alert)

    if new_alerts:
        try:
            from apps.notifications.services.security_events import (
                record_security_alerts,
            )
            record_security_alerts(website, new_alerts)
        except Exception as exc:  # pragma: no cover — notify must never fail a scan
            logger.warning("brand security notification failed for %s: %s", website.id, exc)
        try:
            from apps.brand_vault.services import pulse_agent

            pulse_agent.push_new_high_alerts(website, new_alerts)
        except Exception as exc:  # pragma: no cover — notify must never fail a scan
            logger.warning("brand pulse alert push failed for %s: %s", website.id, exc)

    return created
