"""Security-perception probes.

A security probe is an ordinary ``LLMRankingAudit`` with
``probe_kind == "security"``. Three things differ from a visibility audit:

* the prompts NAME the brand and ask about compliance, incidents, data
  handling, trust, weaknesses and privacy (the ``security`` prompt pack,
  or the customer's saved prompts tagged ``security``);
* the providers are queried COLD: no crawled site context, no RAG block,
  so the answer is what a real user would get;
* every answer gets a second extraction pass that records the security
  claims the model made, whether it used fear framing, and whether it
  advised against the brand.

This module owns prompt selection and the "start a probe" entry point so
the audit endpoint and the Brand Security page build identical audits.
"""
from __future__ import annotations

import logging

from django.conf import settings

from apps.llm_ranking.models import LLMRankingAudit

logger = logging.getLogger("apps")

SECURITY_TAG = "security"


def _has_security_tag(item: dict) -> bool:
    return any(
        str(t).strip().lower() == SECURITY_TAG for t in (item.get("tags") or [])
    )


def _first_competitor(website) -> str:
    for entry in (getattr(website, "competitors", None) or []):
        if isinstance(entry, dict):
            name = (entry.get("name") or "").strip()
        else:
            name = str(entry or "").strip()
        if name:
            return name
    return ""


def security_prompts_for(website, user, *, business_name: str = "", industry: str = "") -> list[dict]:
    """The prompt list a security probe runs for ``website``.

    Saved prompts the customer tagged ``security`` win: they chose the
    wording. Otherwise the security pack is sampled for the brand. Returns
    an empty list only when there is no brand name to ask about; callers
    turn that into a refusal.
    """
    from apps.llm_ranking.services.audit_runner import (
        NoSavedPromptsError,
        gather_saved_prompts,
    )
    from apps.llm_ranking.services.prompt_library import PROBE_KIND_SECURITY, PromptLibrary
    from core.utils.constants import max_prompts_for_user

    try:
        saved = gather_saved_prompts(website, user)
    except NoSavedPromptsError:
        saved = []
    tagged = [p for p in saved if _has_security_tag(p)]
    if tagged:
        return tagged

    brand = (business_name or getattr(website, "name", "") or "").strip()
    try:
        cap = max_prompts_for_user(user)
    except Exception:
        cap = PromptLibrary.DEFAULT_MAX
    return PromptLibrary.generate(
        industry=industry or getattr(website, "industry", "") or "",
        business_name=brand,
        probe_kind=PROBE_KIND_SECURITY,
        competitor=_first_competitor(website),
        max_prompts=cap,
    )


def start_security_probe(
    website,
    user,
    *,
    providers: list[str] | None = None,
    custom_prompts: list[str] | None = None,
) -> LLMRankingAudit:
    """Create and dispatch a security probe for ``website``.

    Raises ``ValueError`` when no prompts can be built (no brand name).
    The prompt-allowance check inside ``create_audit`` still applies.
    """
    from apps.llm_ranking.services.audit_factory import create_audit
    from apps.llm_ranking.services.prompt_library import funnel_stage_for, rationale_for

    business_name = (getattr(website, "name", "") or "").strip()
    industry = getattr(website, "industry", "") or ""

    if custom_prompts:
        prompts = [
            {
                "text": p,
                "type": "custom",
                "tags": [SECURITY_TAG],
                "funnel_stage": funnel_stage_for("custom"),
                "rationale": rationale_for(
                    "custom", business_name=business_name, industry=industry,
                ),
            }
            for p in custom_prompts if p and str(p).strip()
        ]
    else:
        prompts = security_prompts_for(
            website, user, business_name=business_name, industry=industry,
        )
    if not prompts:
        raise ValueError(
            "No security prompts could be built: the website has no name to ask about.",
        )

    audit = create_audit(
        website=website,
        user=user,
        prompts=prompts,
        providers=providers,
        business_name=business_name,
        industry=industry,
        region="global",
        prompt_source="vault",
        probe_kind=LLMRankingAudit.PROBE_KIND_SECURITY,
    )
    if not getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
        from apps.llm_ranking.services.scan_dispatch import dispatch_scan
        dispatch_scan(str(audit.id))
    return audit


def latest_security_audit(website):
    """Most recent completed security probe, or None."""
    return (
        LLMRankingAudit.objects
        .filter(
            website=website,
            probe_kind=LLMRankingAudit.PROBE_KIND_SECURITY,
            status=LLMRankingAudit.STATUS_COMPLETED,
        )
        .order_by("-completed_at")
        .first()
    )
