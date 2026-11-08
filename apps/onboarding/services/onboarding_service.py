"""
Onboarding orchestrator.

Single entry point for the post-register flow: take a URL, return
everything we know about the business so the user only has to confirm
or tweak rather than type from scratch.

Steps:
    1. ``apps.llm_ranking.services.domain_scanner.scan_domain``
       — homepage scrape: name, description, products / features /
       selling points, an industry hint ("" when unknown) and a bounded
       excerpt of the page's visible text
    1b. ``_extract_brand_profile`` — one Claude pass over that excerpt for
       industry, offerings, audience and voice. Every value must be backed
       by the page's own text; anything unsupported stays empty so the
       wizard shows it as missing. It only fills what step 1 left empty.
    1c. ``_safe_site_logo`` — the site's logo URL (cached crawler)
    2. ``_polish_description`` — short Claude pass that rewrites the meta
       description into 1-2 sentences; the scraped text is used as-is
       without a Claude key
    0. ``serp_discovery.GoogleLookup`` — real Google searches (SerpAPI or
       ScraperAPI) for "<brand> alternatives", "<brand> competitors" and
       "best <industry>", started right after step 1 so they run while
       the Claude passes do
    3. ``_llm_topics`` — up to 10 buyer questions: Google's People also ask
       questions and related searches, rounded out by Claude. Without a
       Claude key Google's own questions are used as they are; with
       neither, topics stay empty with ``topics_source="unavailable"`` and
       ``missing_keys``. Nothing is invented.
    4. ``_safe_discover_competitors`` — from Google's results when they
       are usable (Claude may only pick companies those results show),
       else Claude from memory, else the legacy Google Custom Search

Each competitor and topic says where it came from: "search" (Google's
results alone), "ai" (Claude alone) or "both" (Google's results, picked or
worded by Claude).

The scan only previews: nothing is saved onto a website here. A failure
in any step leaves that field empty so the user can fill it by hand.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from urllib.parse import urlparse

from apps.onboarding.services import serp_discovery
from core.llm import ClaudeUtility

logger = logging.getLogger("apps")

# Field caps, matching the wizard's LIMITS (useOnboardingFlow.js) so what
# the scan returns is never clipped again on the way in.
INDUSTRY_MAX = 100
OFFERING_MAX = 100
OFFERINGS_MAX = 8
AUDIENCE_MAX = 600
VOICE_MAX = 300
# An evidence quote shorter than this proves nothing ("the", "AI").
EVIDENCE_MIN = 8
EVIDENCE_MIN_WORDS = 3
# The business name as it goes into a prompt.
NAME_PROMPT_MAX = 100
TOPICS_MAX = 10
TOPIC_MAX = 300

# Words that carry no meaning of their own. A grounded value's other words
# must all appear in its evidence quote.
STOP_WORDS = frozenset("""
    a an the and or but nor of for to in on at by with from into onto over
    under about as per via than then so that this these those it its it's is
    are was were be been being has have had do does did not no yes we our us
    you your they their them he she his her who whom whose which what when
    where why how all any each every some such more most other own same very
    can will just also only both either neither up down out off again further
    once here there s t
""".split())


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
    # Logo URL from the site itself ("" when none could be found).
    logo_url: str = ""

    # Description: ``description_short`` is the 1-2 sentence polished
    # version intended for display. ``description_raw`` is whatever the
    # scraper extracted from meta tags / og:description.
    description_short: str = ""
    description_raw: str = ""

    # Inventory derived from the homepage
    products: list[str] = field(default_factory=list)
    features: list[str] = field(default_factory=list)
    selling_points: list[str] = field(default_factory=list)
    # Who the site says it serves, and how its own copy sounds. Empty
    # unless the page's text supports a value.
    audience: str = ""
    voice: str = ""
    # Up to 10 buyer questions. ``topics_source`` sums up where they came
    # from: "search" (Google's own questions), "ai" (drafted by Claude),
    # "both" (a mix, or Google's searches worded by Claude). Empty topics
    # say why: "unavailable" (no Claude key and nothing usable from Google,
    # see ``missing_keys``) or "failed" (the call failed or there was
    # nothing to draft from).
    topics: list[str] = field(default_factory=list)
    topics_source: str = ""
    missing_keys: list[str] = field(default_factory=list)
    # Parallel arrays (same length as `topics`):
    # - topic_brands: the competitor brand names Claude expects in an AI
    #   answer to that question ([] for Google-only rows).
    # - topic_sources: "search", "ai" or "both" for each topic.
    # - topic_evidence: the Google text a topic came from,
    #   {kind: "question"|"related", text, query}, or None for "ai".
    topic_brands: list[list[str]] = field(default_factory=list)
    topic_sources: list[str] = field(default_factory=list)
    topic_evidence: list[dict | None] = field(default_factory=list)

    # Discovered competitors {name, domain, source: "search"|"ai"|"both",
    # evidence: {query, rank, url} | None}. ``competitors_source`` is the
    # list's source, or "unavailable" (nothing configured to find them) or
    # "failed" (none found) when it is empty.
    competitors: list[dict] = field(default_factory=list)
    competitors_source: str = ""

    # The Google lookup: {configured, provider, label, error, searches,
    # queries: [{query, purpose, error, results}]}.
    google: dict = field(default_factory=dict)


def run_onboarding_scan(url: str, *, user=None) -> OnboardingPayload:
    """Scan + polish + discover, returning a single payload for the UI.

    ``user`` is who the Claude calls are recorded and allowance-checked
    against, like every other AI feature.
    """
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

    # Google first, so its searches run while Claude works below.
    lookup = serp_discovery.GoogleLookup.start(
        business_name=payload.business_name,
        seed=payload.industry or (payload.products[0] if payload.products else ""),
        user=user,
    )

    # Grounded profile: fills only what the heuristic scan left empty.
    profile = _extract_brand_profile(
        business_name=payload.business_name,
        page_text=scan.get("page_text") or "",
        user=user,
    )
    if not payload.industry:
        payload.industry = profile["industry"]
    if not payload.products:
        payload.products = profile["offerings"]
    payload.audience = profile["audience"]
    payload.voice = profile["voice"]
    # The buyer-question search, when only the profile gave a seed for it.
    lookup.submit_seed(payload.industry or (payload.products[0] if payload.products else ""))

    payload.logo_url = _safe_site_logo(payload.domain)

    payload.description_short = _polish_description(
        business_name=payload.business_name,
        industry=payload.industry,
        raw_description=payload.description_raw,
        selling_points=payload.selling_points,
        products=payload.products,
        user=user,
    )

    google = lookup.collect()
    payload.google = google.block()

    claude_ready = ClaudeUtility(model="claude-haiku-4-5").is_configured()
    if not claude_ready:
        payload.missing_keys = ["ANTHROPIC_API_KEY"]
    questions, related = google.questions(), google.related()
    topic_rows: list[dict] = []
    if claude_ready:
        topic_rows = _llm_topics(
            business_name=payload.business_name,
            industry=payload.industry,
            description=payload.description_short or payload.description_raw,
            selling_points=payload.selling_points,
            products=payload.products,
            google_questions=questions,
            google_related=related,
            user=user,
        )
    if not topic_rows:
        # Google's own questions, as they are, rather than nothing.
        topic_rows = _google_topics(questions, related)
    payload.topics = [row["topic"] for row in topic_rows]
    payload.topic_brands = [row.get("brands", []) for row in topic_rows]
    payload.topic_sources = [row["source"] for row in topic_rows]
    payload.topic_evidence = [row.get("evidence") for row in topic_rows]
    if topic_rows:
        payload.topics_source = _overall_source(payload.topic_sources)
    else:
        payload.topics_source = "failed" if claude_ready else "unavailable"

    # Best-effort, like everything else here: a crash in discovery must
    # degrade to an empty list, never abort the scan the user is
    # waiting on.
    try:
        competitors = _safe_discover_competitors(
            business_name=payload.business_name,
            industry=payload.industry,
            domain=payload.domain,
            description=payload.description_short or payload.description_raw,
            google=google,
            user=user,
        )
    except Exception as exc:
        logger.warning("Competitor discovery failed: %s", exc)
        competitors = []
    payload.competitors = [_competitor_row(c) for c in competitors or [] if isinstance(c, dict)]
    if payload.competitors:
        payload.competitors_source = _overall_source(
            [c["source"] for c in payload.competitors if c["source"]]
        )
    else:
        anything = google.configured or claude_ready or _legacy_search_configured()
        payload.competitors_source = "failed" if anything else "unavailable"

    return payload


def _overall_source(sources: list[str]) -> str:
    """One label for a list: its single source, else "both"."""
    kinds = set(sources)
    if not kinds:
        return ""
    return kinds.pop() if len(kinds) == 1 else serp_discovery.SOURCE_BOTH


def _competitor_row(raw: dict) -> dict:
    evidence = raw.get("evidence")
    return {
        "name": raw.get("name") or "",
        "domain": raw.get("domain") or "",
        "source": raw.get("source") or "",
        "evidence": evidence if isinstance(evidence, dict) else None,
    }


def _legacy_search_configured() -> bool:
    from django.conf import settings

    return bool(getattr(settings, "GOOGLE_SEARCH_API_KEY", "")
                and getattr(settings, "GOOGLE_SEARCH_ENGINE_ID", ""))


def _prompt_name(business_name: str) -> str:
    """The business name as it goes into a prompt: sanitized, at most
    NAME_PROMPT_MAX characters."""
    from core.text_sanitizer import sanitize_untrusted_text

    return _clip(sanitize_untrusted_text(business_name or ""), NAME_PROMPT_MAX) if business_name else ""


def _domain_of(url: str) -> str:
    try:
        return urlparse(url if url.startswith("http") else f"https://{url}").netloc
    except Exception:
        return ""


def _empty_profile() -> dict:
    return {"industry": "", "offerings": [], "audience": "", "voice": ""}


def _norm(text: str) -> str:
    """Comparison form for grounding checks: sanitized, lowercase, one space."""
    from core.text_sanitizer import sanitize_untrusted_text

    return re.sub(r"\s+", " ", sanitize_untrusted_text(text or "")).strip().lower()


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return cut or text[:limit]


_WORD_RE = re.compile(r"[^\W_]+")


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(_norm(text))


def _word_forms(word: str) -> set[str]:
    """``word`` and its forms without a plural or -ing/-ed ending, so
    "tutors", "tutoring" and "tutor" match each other."""
    forms = {word}
    for suffix, replacement in (("ies", "y"), ("ing", ""), ("ed", ""), ("es", ""), ("s", "")):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            forms.add(word[: len(word) - len(suffix)] + replacement)
    return forms


def _words_in_quote(value: str, quote: str) -> bool:
    """True when every content word of ``value`` (stop-words removed)
    appears in ``quote``. A value with no content words is not grounded."""
    content = [w for w in _words(value) if w not in STOP_WORDS]
    if not content:
        return False
    have: set[str] = set()
    for word in _words(quote):
        have |= _word_forms(word)
    return all(_word_forms(word) & have for word in content)


def _grounded_value(raw, page: str, limit: int) -> str:
    """Return ``raw["value"]`` sanitized and clipped, or "" unless
    ``raw["evidence"]`` is a verbatim quote from the page of at least
    EVIDENCE_MIN_WORDS words that contains every content word of the
    value."""
    from core.text_sanitizer import sanitize_untrusted_text

    if not isinstance(raw, dict):
        return ""
    value, evidence = raw.get("value"), raw.get("evidence")
    if not isinstance(value, str) or not isinstance(evidence, str):
        return ""
    quote = _norm(evidence).strip("\"'.… ")
    if len(quote) < EVIDENCE_MIN or len(_words(quote)) < EVIDENCE_MIN_WORDS:
        return ""
    if quote not in page:
        return ""
    value = sanitize_untrusted_text(value)
    if not _words_in_quote(value, quote):
        return ""
    return _clip(value, limit)


def _names_on_page(name_key: str, page: str) -> bool:
    """True when ``name_key`` appears in ``page`` as whole words."""
    return re.search(rf"(?<!\w){re.escape(name_key)}(?!\w)", page) is not None


def _grounded_offerings(raw, page: str, business_name: str = "") -> list[str]:
    """Offerings the page names itself, deduped and capped. The brand's own
    name is not an offering."""
    from core.text_sanitizer import sanitize_untrusted_text

    if not isinstance(raw, list):
        return []
    seen: set[str] = {_norm(business_name)} if business_name else set()
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        name = re.sub(r"\s+", " ", sanitize_untrusted_text(item)).strip().strip("\"'.,;:")
        key = _norm(name)
        if (len(name) < 3 or len(name) > OFFERING_MAX or key in seen
                or not _names_on_page(key, page)):
            continue
        seen.add(key)
        out.append(name)
        if len(out) >= OFFERINGS_MAX:
            break
    return out


def _extract_brand_profile(*, business_name: str, page_text: str, user=None) -> dict:
    """Industry, offerings, audience and voice, grounded in ``page_text``.

    One Claude call over the homepage's own visible text. The model must
    quote the page for each value; a value whose quote is not found in the
    text, or an offering the text does not name, is dropped. Returns empty
    values when Claude is not configured, the page gave no text, or the
    call or its JSON fails: the wizard then shows those fields as missing
    for the user to fill. Never raises.
    """
    profile = _empty_profile()
    if not (page_text or "").strip():
        return profile
    provider = ClaudeUtility(model="claude-haiku-4-5", max_tokens=700)
    if not provider.is_configured():
        # No API key is the normal fallback path in dev/tests — stay silent.
        return profile
    try:
        from apps.llm_ranking.services.ranking_service import build_enriched_system_prompt

        system_prompt = build_enriched_system_prompt(
            "You read a company's homepage text and report only what that "
            "text itself states. You never guess, infer from general "
            "knowledge, or fill gaps.",
            page_text,
        )
        prompt = (
            f"The homepage text in the system prompt belongs to "
            f"{_prompt_name(business_name) or 'this business'}. From that text only, report:\n"
            "- industry: the market or category the business is in, as a "
            "short phrase (max 6 words).\n"
            "- offerings: names of the products, plans or services the text "
            "actually names, copied exactly as written (max 8). Not generic "
            "features, slogans or navigation links.\n"
            "- audience: who the text says the business serves, in one or "
            "two plain sentences.\n"
            "- voice: how the text's own copy sounds, as a short phrase "
            "(e.g. 'friendly and plain-spoken').\n\n"
            "Every value needs evidence: for industry, audience and voice, "
            "give a quote of at least 3 words copied word for word from the "
            "text that supports it. Every word of a value, apart from small "
            "words like 'and' or 'for', must appear in its quote, so build "
            "each value from the text's own words. If the text does not "
            "clearly support a value, "
            "return an empty string (or an empty list for offerings). An "
            "empty value is always better than a guess.\n\n"
            "Return ONLY a JSON object, no prose, no code fences:\n"
            '{"industry": {"value": "", "evidence": ""}, '
            '"offerings": [], '
            '"audience": {"value": "", "evidence": ""}, '
            '"voice": {"value": "", "evidence": ""}}'
        )
        result = provider.query(
            prompt, system_prompt,
            module="onboarding", role="brand_profile", user=user,
        )
        if not result.succeeded:
            logger.debug("Brand profile extraction skipped: %s", result.error)
            return profile
        match = re.search(r"\{.*\}", result.text or "", re.DOTALL)
        if not match:
            return profile
        data = json.loads(match.group())
        if not isinstance(data, dict):
            return profile

        page = _norm(page_text)
        profile["industry"] = _grounded_value(data.get("industry"), page, INDUSTRY_MAX)
        profile["offerings"] = _grounded_offerings(
            data.get("offerings"), page, business_name,
        )
        profile["audience"] = _grounded_value(data.get("audience"), page, AUDIENCE_MAX)
        profile["voice"] = _grounded_value(data.get("voice"), page, VOICE_MAX)
        return profile
    except Exception as exc:
        logger.debug("Brand profile extraction skipped: %s", exc)
        return _empty_profile()


def _safe_site_logo(domain: str) -> str:
    """The site's logo URL via the cached logo crawler; "" on any failure."""
    if not domain:
        return ""
    try:
        from apps.prompt_library.services.logo_crawler import fetch_site_logo

        logo = fetch_site_logo(domain)
    except Exception as exc:
        logger.debug("Logo lookup skipped for %s: %s", domain, exc)
        return ""
    return logo if isinstance(logo, str) else ""


def _polish_description(
    *,
    business_name: str,
    industry: str,
    raw_description: str,
    selling_points: list[str],
    products: list[str],
    user=None,
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
        f"Business: {_prompt_name(business_name)}\n"
        f"Industry: {industry}\n"
        f"Existing description: {raw_description}{bullets}\n\n"
        f"Return only the description, no preamble."
    )
    # The gateway records token usage centrally (module="onboarding",
    # role="description_polish" — same keys the old inline block wrote).
    # Charged to the scanning user; no website exists yet.
    result = provider.query(prompt, module="onboarding", role="description_polish", user=user)
    if not result.succeeded:
        logger.debug("Description polish skipped: %s", result.error)
        return fallback
    text = result.text.strip().strip('"')
    return text or fallback


def _topic_text(raw) -> str:
    return serp_discovery.clean_text(raw, TOPIC_MAX).rstrip(".,")


def _topic_inputs(questions: list[dict], related: list[dict]) -> dict[str, dict]:
    """Folded Google text -> its evidence {kind, text, query}."""
    inputs: dict[str, dict] = {}
    for kind, rows in (("question", questions), ("related", related)):
        for row in rows or []:
            key = serp_discovery.fold(row.get("text"))
            if key and key not in inputs:
                inputs[key] = {"kind": kind, "text": row["text"], "query": row.get("query", "")}
    return inputs


def _topic_origin(topic: str, claimed, based_on, inputs: dict[str, dict]) -> tuple[str, dict | None]:
    """(source, evidence) for one drafted topic, checked against Google's
    text: Google's own words are "search", Google's text reworded by Claude
    is "both", anything else is "ai"."""
    exact = inputs.get(serp_discovery.fold(topic))
    if exact:
        return serp_discovery.SOURCE_SEARCH, exact
    if claimed in ("question", "related") and isinstance(based_on, str):
        origin = inputs.get(serp_discovery.fold(based_on))
        if origin:
            return serp_discovery.SOURCE_BOTH, origin
    return serp_discovery.SOURCE_AI, None


def _google_topics(questions: list[dict], related: list[dict]) -> list[dict]:
    """Buyer questions from Google alone: People also ask as written, then
    related searches of at least 4 words. Used without Claude."""
    out: list[dict] = []
    seen: set[str] = set()
    for kind, rows in (("question", questions), ("related", related)):
        for row in rows or []:
            topic = _topic_text(row.get("text"))
            key = serp_discovery.fold(topic)
            if len(topic) < 6 or key in seen:
                continue
            if kind == "related" and len(topic.split()) < 4:
                continue
            seen.add(key)
            out.append({
                "topic": topic, "brands": [], "source": serp_discovery.SOURCE_SEARCH,
                "evidence": {"kind": kind, "text": row["text"], "query": row.get("query", "")},
            })
            if len(out) >= TOPICS_MAX:
                return out
    return out


def _llm_topics(
    *,
    business_name: str,
    industry: str,
    description: str,
    selling_points: list[str],
    products: list[str],
    google_questions: list[dict] | None = None,
    google_related: list[dict] | None = None,
    user=None,
) -> list[dict]:
    """
    Ask Claude Haiku for 10 buyer questions, starting from Google's own
    People also ask questions and related searches when there are any, plus
    the brand names the audit will measure against for each.

    Returns a list of dicts:
        [{"topic": "best provider credentialing services for clinics",
          "brands": ["Verifiable", "Medallion", "Andros"],
          "source": "search" | "ai" | "both",
          "evidence": {"kind", "text", "query"} | None}, ...]

    A topic counts as Google's ("search" or "both") only when it, or the
    Google text it names, matches what Google returned.

    Returns [] when nothing could be drafted; the caller reports that
    instead of substituting topics.
    """
    if not (business_name or description):
        return []
    provider = ClaudeUtility(model="claude-haiku-4-5", max_tokens=1400)
    if not provider.is_configured():
        # No API key is the normal fallback path in dev/tests — stay silent.
        return []
    questions = list(google_questions or [])
    related = list(google_related or [])
    inputs = _topic_inputs(questions, related)
    try:
        from apps.llm_ranking.services.ranking_service import build_enriched_system_prompt

        bullets = ""
        if selling_points:
            bullets += "\nKey selling points:\n- " + "\n- ".join(selling_points[:5])
        if products:
            bullets += "\nProducts / services offered:\n- " + "\n- ".join(products[:5])

        google_lines = [f"Question: {row['text']}" for row in questions]
        google_lines += [f"Related search: {row['text']}" for row in related]
        system_prompt = build_enriched_system_prompt(
            "You write the questions real buyers ask AI assistants when they "
            "look for a business like the one described.",
            ("Google results for searches about this market:\n" + "\n".join(google_lines))
            if google_lines else "",
        )
        google_rules = (
            "7. Google's 'People also ask' questions and related searches "
            "are listed in the system prompt. Use Google's questions word "
            "for word first when they fit this business (source "
            '"question", "from" = the question exactly). Rewrite related '
            "searches that fit into natural sentences (source \"related\", "
            '"from" = the related search exactly). Draft the rest yourself '
            '(source "ai", "from" null).\n\n'
        ) if google_lines else (
            '7. Set "source" to "ai" and "from" to null.\n\n'
        )

        prompt = (
            "You are generating buyer-intent search queries that real "
            "people would type into ChatGPT, Claude, or Perplexity when "
            "looking for a business like this one. For each query you "
            "also name the competitor brands the audit will track in "
            "the LLM's answer to that query.\n\n"
            f"Business: {_prompt_name(business_name)}\n"
            f"Industry: {industry or 'unspecified'}\n"
            f"Description: {description}{bullets}\n\n"
            f"Return {TOPICS_MAX} entries that satisfy ALL of:\n"
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
            "year references (they age badly). Never name the business "
            "itself.\n\n"
            "6. For 'brands', list 3-5 real, currently-operating "
            "competitor companies most likely to appear in a top-tier "
            "LLM's answer to that query. Use the public-facing brand "
            "name only (e.g. 'Verifiable', not 'verifiable.com'). "
            "Exclude the subject company. Do NOT invent names.\n"
            f"{google_rules}"
            "Return ONLY a JSON array of objects, no prose, no code "
            "fences. Example:\n"
            '[{"topic": "best provider credentialing services for clinics",'
            ' "brands": ["Verifiable", "Medallion", "Andros"],'
            ' "source": "ai", "from": null}]'
        )

        # The gateway records token usage centrally (module="onboarding",
        # role="topic_generation"), charged to the scanning user.
        result = provider.query(prompt, system_prompt, module="onboarding",
                                role="topic_generation", user=user)
        if not result.succeeded:
            logger.debug("LLM topic generation skipped: %s", result.error)
            return []
        text = result.text

        match = re.search(r"\[.*\]", text or "", re.DOTALL)
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
            claimed, based_on = None, None
            if isinstance(row, str):
                topic = _topic_text(row)
                brands: list[str] = []
            elif isinstance(row, dict):
                topic = _topic_text(row.get("topic"))
                raw_brands = row.get("brands") or []
                brands = [
                    b.strip() for b in raw_brands
                    if isinstance(b, str) and b.strip()
                    and b.strip().lower() != own_brand
                ][:5] if isinstance(raw_brands, list) else []
                claimed, based_on = row.get("source"), row.get("from")
            else:
                continue

            key = topic.lower()
            if len(topic) < 6 or key in seen:
                continue
            seen.add(key)
            source, evidence = _topic_origin(topic, claimed, based_on, inputs)
            out.append({"topic": topic, "brands": brands, "source": source,
                        "evidence": evidence})
            if len(out) >= TOPICS_MAX:
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
    user=None,
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
        prompt = (
            "Name real, currently-operating companies that directly "
            "compete with the business below. Only include companies "
            "you are reasonably sure exist — do not invent names.\n\n"
            f"Business: {_prompt_name(business_name)}\n"
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
        # Charged to the scanning user; no website exists yet.
        result = provider.query(prompt, module="onboarding", role="competitor_discovery",
                                user=user)
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
            name = serp_discovery.clean_text(row.get("name"), 120)
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
    google: serp_discovery.GoogleResults | None = None, user=None,
) -> list[dict]:
    """
    Find competitors for the onboarded website.

    Order:
        1. Google's results (``serp_discovery``), whenever the brand searches
           came back usable. Claude, when configured, may only pick
           companies those results show (source "both"); otherwise the
           result sites are suggested (source "search"). Usable results that
           show no competitor give [], never names from memory.
        2. Claude from memory (source "ai"), when Google is not configured
           or its searches failed.
        3. Google Custom Search (source "search"), for legacy tenants that
           still have a working key (Google closed it to new customers).

    Each row is {name, domain, source, evidence: {query, rank, url} | None}.
    Returns [] if everything fails so the wizard renders the empty state
    rather than aborting.
    """
    if not (business_name or industry):
        return []

    if google is not None and google.has_competitor_results():
        return serp_discovery.competitors_from_results(
            google, business_name=business_name, domain=domain, user=user,
        )

    competitors = _llm_competitors(
        business_name=business_name,
        industry=industry,
        domain=domain,
        description=description,
        user=user,
    )
    if competitors:
        return [{**c, "source": serp_discovery.SOURCE_AI, "evidence": None} for c in competitors]

    # Fallback: Google Custom Search (only useful for tenants still
    # within the legacy CSE window).
    try:
        from apps.llm_ranking.services.competitor_discovery import discover_competitors
        found = discover_competitors(
            business_name=business_name,
            industry=industry,
            domain=domain,
            description=description,
            max_results=8,
        ) or []
    except Exception as exc:
        logger.debug("Google competitor discovery skipped: %s", exc)
        return []
    return [{**c, "source": serp_discovery.SOURCE_SEARCH, "evidence": None} for c in found]


def to_dict(payload: OnboardingPayload) -> dict:
    return asdict(payload)
