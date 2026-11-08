"""LLM judge for Brand Security findings.

The response auditor escalates nuanced detector findings here with the
detector's own ``question`` string. The judge returns a strict-JSON
``{issue, severity, sentiment_score, detail}`` verdict.

Uses Claude Haiku for cheap calls. Without ``ANTHROPIC_API_KEY``, or when
the call fails, it returns the empty verdict (``issue == "none"`` and no
detail), which callers treat as "no judgement" rather than as a verdict.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from apps.brand_vault.models import SafetyAlert
from core.llm import ClaudeUtility

logger = logging.getLogger("apps")


@dataclass
class Verdict:
    """Judge output for a finding. ``issue == "none"`` drops the finding."""

    issue: str = "none"
    severity: str = "none"  # high | medium | low | none
    sentiment_score: float | None = None
    detail: str = ""

# Maximum characters of brand ground-truth to include in a single judge
# prompt. Enough for ~5 medium-sized chunks, well under the model's
# context window.
_MAX_GROUND_TRUTH_CHARS = 4000

_MODEL = "claude-haiku-4-5-20251001"

# Derived from the model choices so a new issue code is valid here the
# moment it exists on SafetyAlert — the old hand-copied set drifted.
_VALID_ISSUES = {code for code, _ in SafetyAlert.ISSUE_CHOICES} | {"none"}
_VALID_SEVERITIES = {"high", "medium", "low", "none"}


def judge_finding(
    *,
    question: str,
    brand: str,
    title: str,
    snippet: str,
    allowed_issues: tuple[str, ...],
    ground_truth: list[dict] | None = None,
    user=None,
    website=None,
) -> Verdict:
    """Ask Claude Haiku to adjudicate one finding. Returns a ``Verdict``.

    ``question`` is detector-specific (e.g. "Is this LLM answer factually
    wrong about the brand?"). ``allowed_issues`` narrows the JSON contract
    so the caller only gets back issue classes it can raise.

    ``ground_truth`` is an optional list of retrieval hits from the
    client's own RAG knowledge base (dicts with at least ``text`` and
    ``section_label``). When provided, the judge is told to prefer the
    client's stated facts over its own priors, which converts vague
    "does this sound off" adjudication into concrete contradiction
    checking against the client's uploaded brand material.

    ``user``/``website`` attribute the call's token spend to the tenant
    via ``core.ai_tracking``; pass them from any call site that runs at
    volume.
    """
    # Instantiate the gateway provider up front: the is_configured() guard
    # keeps the no-API-key dev path from paying for prompt construction.
    provider = ClaudeUtility(model=_MODEL, max_tokens=400)
    if not provider.is_configured():
        return Verdict()

    issue_list = ", ".join(allowed_issues + ("none",))
    ground_truth_block = _format_ground_truth(ground_truth or [])
    if ground_truth_block:
        grounding_preamble = (
            "The client has provided the following statements about their own brand. "
            "Treat these as the source of truth. If the content contradicts, "
            "omits, or misrepresents them, flag it. If the content is consistent, "
            'return issue="none".\n\n'
            f"{ground_truth_block}\n\n"
        )
    else:
        grounding_preamble = (
            "The client has not uploaded any brand reference material yet, so "
            "judge using general reasoning only. Be conservative: prefer "
            'issue="none" unless the content is clearly wrong or harmful.\n\n'
        )

    prompt = (
        f"You judge whether a piece of content is a brand-safety issue for the brand \"{brand}\".\n"
        f"Question: {question}\n\n"
        f"{grounding_preamble}"
        f"Content title: {title}\n"
        f"Content snippet:\n{snippet}\n\n"
        "Respond with ONE JSON object and no prose. Schema:\n"
        "{\n"
        f'  "issue": one of [{issue_list}],\n'
        '  "severity": one of [high, medium, low, none],\n'
        '  "sentiment_score": float between -1 and 1 (negative = hostile to the brand),\n'
        '  "detail": one short sentence explaining the verdict, citing the brand'
        ' fact that was contradicted when applicable\n'
        "}\n"
        'Use issue="none" and severity="none" if there is no brand-safety issue.'
    )

    # The gateway records token usage centrally (module="brand_security",
    # role="judge" — same keys the old inline record_usage block wrote).
    result = provider.query(
        prompt,
        user=user,
        website=website,
        module="brand_security",
        role="judge",
    )
    if not result.succeeded:
        logger.warning("Brand security judge call failed: %s", result.error)
        return Verdict()

    return _parse_verdict(result.text, allowed_issues)


def _format_ground_truth(hits: list[dict]) -> str:
    """Render retrieval hits as a bounded ``=== BRAND GROUND TRUTH ===`` block."""
    if not hits:
        return ""
    lines = ["=== BRAND GROUND TRUTH ==="]
    used = len(lines[0]) + 1
    for i, h in enumerate(hits, start=1):
        label = (h.get("section_label") or h.get("source_url") or "brand source")[:120]
        text = (h.get("text") or "").strip()
        if not text:
            continue
        block = f"[{i}] {label}\n{text}"
        if used + len(block) > _MAX_GROUND_TRUTH_CHARS:
            break
        lines.append(block)
        used += len(block) + 1
    if len(lines) == 1:
        return ""
    return "\n\n".join(lines)


def _parse_verdict(raw: str, allowed_issues: tuple[str, ...]) -> Verdict:
    if not raw:
        return Verdict()
    candidates = [raw]
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    if fence:
        candidates.append(fence.group(1))
    brace = re.search(r"\{.*\}", raw, re.DOTALL)
    if brace:
        candidates.append(brace.group(0))

    for cand in candidates:
        try:
            data = json.loads(cand)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        issue = str(data.get("issue") or "none").strip().lower()
        severity = str(data.get("severity") or "none").strip().lower()
        allowed = set(allowed_issues) | {"none"}
        if issue not in allowed or issue not in _VALID_ISSUES:
            issue = "none"
        if severity not in _VALID_SEVERITIES:
            severity = "none"
        sentiment_raw = data.get("sentiment_score")
        sentiment: float | None
        try:
            sentiment = float(sentiment_raw) if sentiment_raw is not None else None
        except (TypeError, ValueError):
            sentiment = None
        detail = str(data.get("detail") or "").strip()[:600]
        return Verdict(
            issue=issue,
            severity=severity,
            sentiment_score=sentiment,
            detail=detail,
        )
    return Verdict()
