"""
Onboarding orchestrator.

Single entry point for the post-register flow: take a URL, return
everything we know about the business so the user only has to confirm
or tweak rather than type from scratch.

Steps:
    1. ``apps.llm_ranking.services.domain_scanner.scan_domain``
       — homepage scrape: name, description, products / features /
       selling points, and an industry hint ("" when unknown)
    2. ``_polish_description`` — short Claude pass that rewrites the meta
       description into 1-2 sentences; the scraped text is used as-is
       without a Claude key
    3. ``_llm_topics`` — Claude drafts buyer-intent topics. Without a
       Claude key topics stay empty with ``topics_source="unavailable"``
       and ``missing_keys``; nothing is substituted
    4. ``_safe_discover_competitors`` — Claude, then Google Custom Search

The scan only previews: nothing is saved onto a website here. A failure
in any step leaves that field empty so the user can fill it by hand.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from urllib.parse import urlparse

from core.llm import ClaudeUtility

logger = logging.getLogger("apps")


@dataclass
class OnboardingPayload:
    """What we hand back to the wizard so it can render Step 2 + 3."""

    url: str
    success: bool = False
    error: str | None = None

    # Identity
    business_name: str = ""
    domain: str = ""
    industry: str = ""

    # Description: ``description_short`` is the 1-2 sentence polished
    # version intended for display. ``description_raw`` is whatever the
    # scraper extracted from meta tags / og:description.
    description_short: str = ""
    description_raw: str = ""

    # Inventory derived from the homepage
    products: list[str] = field(default_factory=list)
    features: list[str] = field(default_factory=list)
    selling_points: list[str] = field(default_factory=list)
    # Buyer-intent topics drafted by Claude. Empty when they could not be
    # drafted; ``topics_source`` says why: "ai" (drafted), "unavailable"
    # (no Claude key, see ``missing_keys``) or "failed" (the call failed
    # or the site gave nothing to draft from).
    topics: list[str] = field(default_factory=list)
    topics_source: str = ""
    missing_keys: list[str] = field(default_factory=list)
    # Parallel array (same length as `topics`): for each topic, the
    # competitor brand names the audit will look for in LLM responses
    # to that query. Shown under each chip on step 2 so the user knows
    # what they're being benchmarked against.
    topic_brands: list[list[str]] = field(default_factory=list)

    # Discovered competitors {name, domain}
    competitors: list[dict] = field(default_factory=list)


def run_onboarding_scan(url: str) -> OnboardingPayload:
    """Scan + polish + discover, returning a single payload for the UI."""
    from apps.llm_ranking.services.domain_scanner import scan_domain

    payload = OnboardingPayload(url=url)

    scan = scan_domain(url)
    if not scan.get("success"):
        payload.error = scan.get("error") or "Could not scan this domain."
        return payload

    payload.success = True
    payload.business_name = scan.get("business_name") or ""
    payload.industry = scan.get("industry") or ""
    payload.domain = scan.get("domain") or _domain_of(url)
    payload.description_raw = scan.get("description") or ""
    payload.products = list(scan.get("products") or [])[:8]
    payload.features = list(scan.get("features") or [])[:8]
    payload.selling_points = list(scan.get("selling_points") or [])[:6]

    payload.description_short = _polish_description(
        business_name=payload.business_name,
        industry=payload.industry,
        raw_description=payload.description_raw,
        selling_points=payload.selling_points,
        products=payload.products,
    )

    # Topics come only from Claude, which also names the brands the audit
    # will measure against per topic. Without it the list stays empty.
    if not ClaudeUtility(model="claude-haiku-4-5").is_configured():
        payload.topics_source = "unavailable"
        payload.missing_keys = ["ANTHROPIC_API_KEY"]
    else:
        llm_topic_rows = _llm_topics(
            business_name=payload.business_name,
            industry=payload.industry,
            description=payload.description_short or payload.description_raw,
            selling_points=payload.selling_points,
            products=payload.products,
        )
        payload.topics_source = "ai" if llm_topic_rows else "failed"
        payload.topics = [row["topic"] for row in llm_topic_rows]
        payload.topic_brands = [row.get("brands", []) for row in llm_topic_rows]

    # Best-effort, like everything else here: a crash in discovery must
    # degrade to an empty list, never abort the scan the user is
    # waiting on.
    try:
        payload.competitors = _safe_discover_competitors(
            business_name=payload.business_name,
            industry=payload.industry,
            domain=payload.domain,
            description=payload.description_short or payload.description_raw,
        )
    except Exception as exc:
        logger.warning("Competitor discovery failed: %s", exc)
        payload.competitors = []

    return payload


def _domain_of(url: str) -> str:
    try:
        return urlparse(url if url.startswith("http") else f"https://{url}").netloc
    except Exception:
        return ""


def _polish_description(
    *,
    business_name: str,
    industry: str,
    raw_description: str,
    selling_points: list[str],
    products: list[str],
) -> str:
    """
    Ask Claude Haiku for a clean 1-2 sentence "what they do" line.

    Falls back to the scraped meta description (truncated) when the LLM
    call isn't available.
    """
    fallback = (raw_description or "").strip()
    if len(fallback) > 280:
        fallback = fallback[:277].rsplit(" ", 1)[0] + "…"

    provider = ClaudeUtility(model="claude-haiku-4-5", max_tokens=128)
    if not provider.is_configured():
        # No API key is the normal fallback path in dev/tests — stay silent.
        return fallback

    bullets = ""
    if selling_points:
        bullets += "\nSelling points:\n- " + "\n- ".join(selling_points[:5])
    if products:
        bullets += "\nProducts:\n- " + "\n- ".join(products[:5])

    prompt = (
        f"Write a clean 1-2 sentence description (max 200 chars) of "
        f"what this business does. Use plain language, no marketing fluff.\n\n"
        f"Business: {business_name}\n"
        f"Industry: {industry}\n"
        f"Existing description: {raw_description}{bullets}\n\n"
        f"Return only the description, no preamble."
    )
    # The gateway records token usage centrally (module="onboarding",
    # role="description_polish" — same keys the old inline block wrote).
    # No user/website yet: the scan runs before the Website row exists.
    result = provider.query(prompt, module="onboarding", role="description_polish")
    if not result.succeeded:
        logger.debug("Description polish skipped: %s", result.error)
        return fallback
    text = result.text.strip().strip('"')
    return text or fallback


def _llm_topics(
    *,
    business_name: str,
    industry: str,
    description: str,
    selling_points: list[str],
    products: list[str],
) -> list[dict]:
    """
    Ask Claude Haiku for 8 buyer-intent search queries + the brand
    names the audit will measure against for each query.

    Returns a list of dicts:
        [{"topic": "best provider credentialing services for clinics",
          "brands": ["Verifiable", "Medallion", "Andros"]}, ...]

    Returns [] when nothing could be drafted; the caller reports that
    instead of substituting topics.
    """
    if not (business_name or description):
        return []
    provider = ClaudeUtility(model="claude-haiku-4-5", max_tokens=1024)
    if not provider.is_configured():
        # No API key is the normal fallback path in dev/tests — stay silent.
        return []
    try:
        import json
        import re

        bullets = ""
        if selling_points:
            bullets += "\nKey selling points:\n- " + "\n- ".join(selling_points[:5])
        if products:
            bullets += "\nProducts / services offered:\n- " + "\n- ".join(products[:5])

        prompt = (
            "You are generating buyer-intent search queries that real "
            "people would type into ChatGPT, Claude, or Perplexity when "
            "looking for a business like this one. For each query you "
            "also name the competitor brands the audit will track in "
            "the LLM's answer to that query.\n\n"
            f"Business: {business_name}\n"
            f"Industry: {industry or 'unspecified'}\n"
            f"Description: {description}{bullets}\n\n"
            "Return 8 entries that satisfy ALL of:\n"
            "1. Each query reads as a natural sentence a human would "
            "actually type — no fragment soup, no broken grammar.\n"
            "2. Pick the right category noun based on what this "
            "business IS. A SaaS product → 'tool' / 'platform' / "
            "'software'. A credentialing service or agency → "
            "'service' / 'provider' / 'company'. A consultancy → "
            "'firm' / 'consultant'. Match the noun to the business; "
            "do not blindly say 'tools' for a non-software business.\n"
            "3. Each query is highly correlated with the description.\n"
            "4. Mix query styles: 'best X for Y', 'how to find Z', "
            "'top alternatives to ...', 'who handles ...', 'compare ...'.\n"
            "5. Each query is 4-12 words. No trailing punctuation. No "
            "year references (they age badly).\n\n"
            "6. For 'brands', list 3-5 real, currently-operating "
            "competitor companies most likely to appear in a top-tier "
            "LLM's answer to that query. Use the public-facing brand "
            "name only (e.g. 'Verifiable', not 'verifiable.com'). "
            "Exclude the subject company. Do NOT invent names.\n\n"
            "Return ONLY a JSON array of objects, no prose, no code "
            "fences. Example:\n"
            '[{"topic": "best provider credentialing services for clinics",'
            ' "brands": ["Verifiable", "Medallion", "Andros"]},'
            ' {"topic": "how to get doctors in-network fast",'
            ' "brands": ["CredSimple", "Medallion", "Andros"]}]'
        )

        # The gateway records token usage centrally (module="onboarding",
        # role="topic_generation" — same keys the old inline block wrote).
        # No user/website yet: the scan runs before the Website row exists.
        result = provider.query(prompt, module="onboarding", role="topic_generation")
        if not result.succeeded:
            logger.debug("LLM topic generation skipped: %s", result.error)
            return []
        text = result.text

        match = re.search(r"\[.*\]", text, re.DOTALL)
        if not match:
            return []
        data = json.loads(match.group())
        if not isinstance(data, list):
            return []

        own_brand = (business_name or "").strip().lower()
        seen: set[str] = set()
        out: list[dict] = []
        for row in data:
            # Backward-compat: tolerate plain strings as topics with
            # no brand info, in case the model regresses on format.
            if isinstance(row, str):
                topic = row.strip().rstrip(".,")
                brands: list[str] = []
            elif isinstance(row, dict):
                topic = (row.get("topic") or "").strip().rstrip(".,")
                raw_brands = row.get("brands") or []
                brands = [
                    b.strip() for b in raw_brands
                    if isinstance(b, str) and b.strip()
                    and b.strip().lower() != own_brand
                ][:5]
            else:
                continue

            key = topic.lower()
            if len(topic) < 6 or key in seen:
                continue
            seen.add(key)
            out.append({"topic": topic, "brands": brands})
            if len(out) >= 8:
                break
        return out
    except Exception as exc:
        logger.debug("LLM topic generation skipped: %s", exc)
        return []


def _llm_competitors(
    *,
    business_name: str,
    industry: str,
    domain: str,
    description: str,
) -> list[dict]:
    """
    Ask Claude Haiku to name 8 real competitors of the business.

    Replaces (or precedes, depending on availability) the Google
    Custom Search competitor discoverer. Google closed Custom Search
    JSON API to new customers in early 2026, so this path is the
    primary source for newly-provisioned tenants; Google is used as
    an optional fallback when an existing tenant still has a working
    key.

    The LLM is well-suited to this task — it doesn't need to "search"
    for Stripe's competitors; it already knows them. Returns
    [{"name", "domain"}, ...] in the same shape the wizard expects.
    """
    if not (business_name or description):
        return []
    provider = ClaudeUtility(model="claude-haiku-4-5", max_tokens=600)
    if not provider.is_configured():
        # No API key is the normal fallback path in dev/tests — stay silent.
        return []
    try:
        import json
        import re

        prompt = (
            "Name real, currently-operating companies that directly "
            "compete with the business below. Only include companies "
            "you are reasonably sure exist — do not invent names.\n\n"
            f"Business: {business_name}\n"
            f"Industry: {industry or 'unspecified'}\n"
            f"Website: {domain or 'unknown'}\n"
            f"Description: {description}\n\n"
            "Return 6-8 competitors. For each, give the public-facing "
            "company name and the primary website domain (e.g. "
            "'stripe.com', not 'https://stripe.com/'). Exclude the "
            "subject company itself from the list.\n\n"
            "Return ONLY a JSON array of objects, no prose, no code "
            "fences. Example:\n"
            '[{"name": "Stripe", "domain": "stripe.com"}, '
            '{"name": "Adyen", "domain": "adyen.com"}]'
        )

        # The gateway records token usage centrally (module="onboarding",
        # role="competitor_discovery" — same keys the old inline block wrote).
        # No user/website yet: the scan runs before the Website row exists.
        result = provider.query(prompt, module="onboarding", role="competitor_discovery")
        if not result.succeeded:
            logger.debug("LLM competitor discovery skipped: %s", result.error)
            return []
        text = result.text

        match = re.search(r"\[.*\]", text, re.DOTALL)
        if not match:
            return []
        data = json.loads(match.group())
        if not isinstance(data, list):
            return []

        # Normalise + dedupe. Drop entries that don't at least have a
        # name; strip protocol from domain just in case.
        own = (domain or "").lower().removeprefix("www.")
        seen: set[str] = set()
        out: list[dict] = []
        for row in data:
            if not isinstance(row, dict):
                continue
            name = (row.get("name") or "").strip()
            if not name:
                continue
            comp_domain = (row.get("domain") or "").strip().lower()
            comp_domain = re.sub(r"^https?://", "", comp_domain).rstrip("/")
            comp_domain = comp_domain.removeprefix("www.")
            if comp_domain and comp_domain == own:
                continue  # skip self
            key = comp_domain or name.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append({"name": name, "domain": comp_domain})
            if len(out) >= 8:
                break
        return out
    except Exception as exc:
        logger.debug("LLM competitor discovery skipped: %s", exc)
        return []


def _safe_discover_competitors(
    *, business_name: str, industry: str, domain: str, description: str,
) -> list[dict]:
    """
    Find competitors for the onboarded website.

    Order:
        1. LLM (Claude Haiku) — primary path, works without third-party APIs.
        2. Google Custom Search — optional fallback for legacy tenants that
           still have a working key (Google closed it to new customers).

    Returns [] if everything fails so the wizard renders the empty
    state rather than aborting.
    """
    if not (business_name or industry):
        return []

    # Primary: LLM
    competitors = _llm_competitors(
        business_name=business_name,
        industry=industry,
        domain=domain,
        description=description,
    )
    if competitors:
        return competitors

    # Fallback: Google Custom Search (only useful for tenants still
    # within the legacy CSE window).
    try:
        from apps.llm_ranking.services.competitor_discovery import discover_competitors
        return discover_competitors(
            business_name=business_name,
            industry=industry,
            domain=domain,
            description=description,
            max_results=8,
        ) or []
    except Exception as exc:
        logger.debug("Google competitor discovery skipped: %s", exc)
        return []


def to_dict(payload: OnboardingPayload) -> dict:
    return asdict(payload)
