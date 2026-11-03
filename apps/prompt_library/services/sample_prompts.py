"""Business-grounded sample-prompt generation.

Reads the site (when the website has no description yet), then asks
Claude for buyer-intent prompts written for what the business actually
does, inferring the right category noun from the real description.

What the scan reads is used for this request only; it is never saved onto
the website. Without a Claude key ``SynthesisNotConfigured`` is raised and
a failed call raises ``SynthesisFailed``: no template list is substituted.
"""
import json
import logging
import re

from apps.prompt_library.services.context_generator import (
    SynthesisFailed,
    SynthesisNotConfigured,
)
from core.llm import ClaudeUtility

logger = logging.getLogger("apps")


def _site_context(website) -> tuple[str, str, list[str]]:
    """Return (industry, description, products) for prompt drafting.

    The website's own fields win. When the description is missing the
    homepage is scanned and its findings are used in memory only.
    """
    industry = (website.industry or "").strip()
    description = (website.description or "").strip()
    if description:
        return industry, description, []

    from apps.llm_ranking.services.domain_scanner import scan_domain

    try:
        scan = scan_domain(website.url)
    except Exception as exc:
        logger.warning("Sample-prompt scan failed for %s: %s", website.url, exc)
        return industry, description, []
    if not scan.get("success"):
        logger.info("Sample-prompt scan unsuccessful for %s", website.url)
        return industry, description, []

    return (
        industry or (scan.get("industry") or "").strip(),
        (scan.get("description") or "").strip(),
        list(scan.get("products") or [])[:8],
    )


def _llm_prompts(*, provider, business_name, industry, description, products,
                 count, user, website) -> list[dict]:
    """Ask Claude for buyer-intent prompt questions grounded in the business.

    Returns register-ready dicts ({"text", "type"}). Raises
    ``SynthesisFailed`` when the call fails or yields no usable prompt.
    """
    bullets = ""
    if products:
        bullets = "\nProducts / offerings:\n- " + "\n- ".join(products[:6])

    prompt = (
        "You generate the exact questions real potential customers type into "
        "ChatGPT, Claude, or Perplexity when looking for a product or service "
        "like this business — so we can measure how often AI recommends it.\n\n"
        f"Business: {business_name}\n"
        f"Category: {industry or '(infer it from the description)'}\n"
        f"What it does: {description}{bullets}\n\n"
        f"Write exactly {count} questions that satisfy ALL of:\n"
        "1. Each is a natural question a human would actually type, ending "
        "with '?'. 6-20 words.\n"
        "2. Use the CORRECT real-world words for THIS business. If it deals "
        "with cars or car data, ask about cars / vehicles / car deals; a "
        "clinic asks about care / providers; a law firm about lawyers. Do "
        "NOT say 'software', 'SaaS', 'platform' or 'tool' unless the business "
        "genuinely is software. Match the noun to what it actually is.\n"
        "3. Do NOT name this business — we measure whether AI brings it up on "
        "its own.\n"
        "4. Cover a mix of intents: a direct recommendation, a comparison, "
        "alternatives to the market leader, a specific need or use-case, a "
        "beginner/overview question, and a buying-decision question.\n"
        "5. No year references and no superlatives like 'best ever'.\n\n"
        "Return ONLY a JSON array of question strings — no prose, no code fences."
    )

    result = provider.query(
        prompt, module="prompt_library", role="sample_generation",
        user=user, website=website,
    )
    if not result.succeeded:
        logger.warning("Sample-prompt generation failed for %s: %s", website.id, result.error)
        raise SynthesisFailed()

    match = re.search(r"\[.*\]", result.text, re.DOTALL)
    try:
        data = json.loads(match.group()) if match else None
    except Exception:
        data = None
    if not isinstance(data, list):
        raise SynthesisFailed()

    seen: set[str] = set()
    items: list[dict] = []
    for row in data:
        if isinstance(row, str):
            text = row
        elif isinstance(row, dict):
            text = row.get("text") or row.get("prompt") or ""
        else:
            continue
        text = (text or "").strip()
        key = text.lower()
        if len(text) < 8 or key in seen:
            continue
        seen.add(key)
        items.append({"text": text, "type": "custom"})
        if len(items) >= count:
            break
    if not items:
        raise SynthesisFailed()
    return items


def build_sample_prompts(website, *, count: int, user) -> dict:
    """Read the site if needed, then generate business-grounded prompts.

    Returns {"items": [...], "industry": str, "source": "llm"|"none"}.
    ``source == "none"`` means there is no description to work from (none
    saved and none readable on the site) — the caller should ask the user
    for one rather than emit a generic list. Raises
    ``SynthesisNotConfigured`` without a Claude key and ``SynthesisFailed``
    when the call fails.
    """
    provider = ClaudeUtility(model="claude-haiku-4-5", max_tokens=1200)
    if not provider.is_configured():
        raise SynthesisNotConfigured()

    industry, description, products = _site_context(website)
    if not description:
        return {"items": [], "industry": industry, "source": "none"}

    items = _llm_prompts(
        provider=provider, business_name=website.name or "", industry=industry,
        description=description, products=products, count=count,
        user=user, website=website,
    )
    return {"items": items, "industry": industry, "source": "llm"}
