"""
Structured extraction of an LLM response into brand mention data.

Uses a cheap model (Claude Haiku) to parse each raw response into a
deterministic JSON object covering the target brand, competitors, sentiment,
and citations. Falls back to the existing heuristic analyser if the LLM
call fails so an audit never gets blocked by an extractor error.

Design notes:
- The expensive tier (the measurement LLMs) stays unchanged.
- Only ~500 input tokens per extraction call; cost is dominated by output JSON.
- Keeping the prompt versioned lets us re-extract historical responses (replay)
  when the prompt improves, without re-querying the expensive models.
"""
import json
import logging
import re

from django.conf import settings

from core.llm import ClaudeUtility

logger = logging.getLogger("apps")

# Cheap, current model for structured extraction. Overridable per
# environment with LLM_EXTRACTION_MODEL.
EXTRACTION_MODEL = getattr(settings, "LLM_EXTRACTION_MODEL", "") or "claude-haiku-4-5"
EXTRACTION_VERSION = "v4"  # v4 adds kind: brand vs generic (informational term)

EXTRACTION_SYSTEM = (
    "You extract structured brand-mention data from AI assistant responses. "
    "Return strict JSON only, no prose. Follow the schema exactly."
)

EXTRACTION_TEMPLATE = """Target brand: "{brand}"
Keywords that identify the brand: {keywords}

Response to analyse:
---
{response}
---

Return JSON only, matching this schema exactly:
{{
  "target_mentioned": bool,
  "target_position": int or null,         // 1 = first in a ranked list, 2 = second, ... null if not in a list
  "target_linked": bool,                  // was the target mentioned with a hyperlink or URL
  "target_sentiment": "positive" | "neutral" | "negative" | "not_mentioned",
  "target_context": str,                  // up to 300 chars around the first target mention, or ""
  "competitors_mentioned": [              // other NAMED entities highlighted by the response
    {{"name": str, "position": int or null, "linked": bool,
      "sentiment": "positive" | "neutral" | "negative",  // how the response portrays THIS entity
      "domain": str or null,              // the entity's official website domain if you know it, e.g. "nike.com", else null
      "kind": "brand" | "generic"}}       // "brand" ONLY for a real company or branded product (e.g. "Titleist", "Callaway", "golfscot.com");
                                          // "generic" for product categories, equipment types, or informational terms
                                          // (e.g. "golf clubs", "running shoes", "accessories", "ball marker")
  ],
  "primary_recommendation": str or null,  // name of the brand the response clearly recommends first, else null
  "citations": [str]                      // any URLs cited
}}"""


def _call_haiku(prompt: str, *, user=None, website=None, audit_id=None,
                idempotency_key=None) -> str:
    """Call Claude Haiku and return the raw text response. Raises on failure.

    Goes through the central LLM gateway, which handles spend-wall,
    rate-limit/breaker, and usage recording (role=extraction).
    ``idempotency_key`` collapses replayed recordings when the calling
    Celery task is redelivered (acks_late).
    """
    result = ClaudeUtility(model=EXTRACTION_MODEL, max_tokens=1024).query(
        prompt,
        system_prompt=EXTRACTION_SYSTEM,
        user=user,
        website=website,
        audit_id=audit_id,
        role="extraction",
        module="llm_ranking",
        idempotency_key=idempotency_key,
    )
    if not result.succeeded:
        raise ValueError(f"Haiku extraction call failed: {result.error}")
    return result.text


def _clean_domain(raw) -> str:
    """Reduce an LLM-supplied website value to a bare host, or "".

    Accepts "https://www.nike.com/x" or "Nike.com" and yields "nike.com".
    Returns "" for nulls or anything that isn't a plausible domain.
    """
    if not raw or not isinstance(raw, str):
        return ""
    d = raw.strip().lower()
    if "//" in d:
        d = d.split("//", 1)[1]
    d = d.split("/", 1)[0].split("@")[-1].split(":")[0]
    if d.startswith("www."):
        d = d[4:]
    # Plausible domain: label(s) + TLD, no spaces.
    if re.match(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$", d):
        return d
    return ""


def _parse_json_object(text: str) -> dict:
    """Extract the first JSON object from a possibly-messy response."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("No JSON object found in extraction response")
    return json.loads(match.group())


def _normalise(raw: dict) -> dict:
    """Coerce an extracted JSON payload into our canonical shape."""
    valid_sentiments = {"positive", "neutral", "negative", "not_mentioned"}
    sentiment = raw.get("target_sentiment")
    if sentiment not in valid_sentiments:
        sentiment = "not_mentioned" if not raw.get("target_mentioned") else "neutral"

    # Hard caps on list sizes — a malformed or hostile LLM response cannot
    # cause us to write tens of MB of competitor / citation entries into a
    # single LLMRankingResult row. Reasonable upper bounds for real data:
    # most responses surface < 20 competitors and < 20 citations.
    MAX_COMPETITORS = 50
    MAX_CITATIONS = 50
    MAX_URL_LEN = 500

    comp_sentiments = {"positive", "neutral", "negative"}
    competitors = []
    for c in (raw.get("competitors_mentioned") or [])[:MAX_COMPETITORS]:
        if not isinstance(c, dict):
            continue
        name = (c.get("name") or "").strip()
        if not name:
            continue
        pos = c.get("position")
        csent = c.get("sentiment")
        domain = _clean_domain(c.get("domain"))
        kind = c.get("kind")
        if kind not in ("brand", "generic"):
            # Older extraction output (pre-v4) carries no kind. A known
            # official domain is strong brand evidence; otherwise stay
            # conservative and keep the historical "brand" reading —
            # display surfaces apply their own fallback heuristics.
            kind = "brand"
        competitors.append({
            "name": name[:200],
            "position": int(pos) if isinstance(pos, int | float) and pos is not None else None,
            "linked": bool(c.get("linked", False)),
            "sentiment": csent if csent in comp_sentiments else "neutral",
            "domain": domain,
            "kind": kind,
        })

    citations = []
    for u in (raw.get("citations") or [])[:MAX_CITATIONS]:
        s = str(u).strip()[:MAX_URL_LEN]
        if s.startswith(("http://", "https://")):
            citations.append(s)

    primary = raw.get("primary_recommendation")
    primary = str(primary).strip()[:200] if primary else ""

    is_mentioned = bool(raw.get("target_mentioned"))
    position = raw.get("target_position")
    position = int(position) if isinstance(position, int | float) and position is not None else None

    return {
        "is_mentioned": is_mentioned,
        "mention_rank": position,
        "sentiment": sentiment,
        "mention_context": (raw.get("target_context") or "")[:300],
        "is_linked": bool(raw.get("target_linked", False)),
        "competitors_mentioned": competitors,
        "primary_recommendation": primary,
        "citations": citations,
    }


class HaikuExtractionService:
    """
    Wraps a Haiku call that turns raw LLM responses into structured fields.

    `extract` always returns a dict with the same shape; on failure it falls
    back to the heuristic analyser so callers never need error handling.
    """

    MODEL = EXTRACTION_MODEL
    VERSION = EXTRACTION_VERSION

    @classmethod
    def extract(
        cls,
        *,
        response_text: str,
        brand_name: str,
        keywords: list,
        user=None,
        website=None,
        audit_id=None,
        idempotency_key=None,
    ) -> dict:
        """Extract structured mention data for `brand_name` from `response_text`.

        Returns a dict with:
          is_mentioned, mention_rank, sentiment, mention_context,
          is_linked, competitors_mentioned, primary_recommendation,
          citations, confidence_score, extraction_model, extraction_version
        """
        if not response_text or not brand_name:
            return cls._empty_result()

        prompt = EXTRACTION_TEMPLATE.format(
            brand=brand_name,
            keywords=json.dumps(list(keywords or [])),
            response=response_text[:6000],  # guard against runaway inputs
        )

        try:
            raw_text = _call_haiku(
                prompt, user=user, website=website, audit_id=audit_id,
                idempotency_key=idempotency_key,
            )
            parsed = _parse_json_object(raw_text)
            result = _normalise(parsed)
            result["confidence_score"] = 92.0 if result["is_mentioned"] else 95.0
            result["extraction_model"] = cls.MODEL
            result["extraction_version"] = cls.VERSION
            return result
        except Exception as exc:
            logger.warning("Haiku extraction failed; falling back to heuristic: %s", exc)
            return cls._fallback(
                response_text=response_text,
                brand_name=brand_name,
                keywords=keywords,
            )

    @staticmethod
    def _empty_result() -> dict:
        return {
            "is_mentioned": False,
            "mention_rank": None,
            "sentiment": "not_mentioned",
            "mention_context": "",
            "is_linked": False,
            "competitors_mentioned": [],
            "primary_recommendation": "",
            "citations": [],
            "confidence_score": 0.0,
            "extraction_model": "",
            "extraction_version": "",
        }

    @classmethod
    def _fallback(cls, *, response_text: str, brand_name: str, keywords: list) -> dict:
        # Import lazily to avoid a circular import at module load.
        from apps.llm_ranking.services.ranking_service import LLMRankingService

        heuristic = LLMRankingService._analyze_mention(
            response_text=response_text,
            business_name=brand_name,
            keywords=keywords,
        )
        heuristic.update({
            "is_linked": False,
            "competitors_mentioned": [],
            "primary_recommendation": "",
            "citations": [],
            "extraction_model": "heuristic",
            "extraction_version": cls.VERSION,
        })
        return heuristic


# ── Security-perception extraction ──────────────────────────────────────
#
# A second, independent pass over the same raw answer for security probes
# (audits with probe_kind == "security" or prompts tagged "security"). It
# does not replace the brand-mention extraction above — mention rate,
# sentiment and competitors are still read from that — it adds the
# claim-level view the perception metrics need: which security statements
# the model made about the brand, with what confidence, and whether the
# answer steers the reader away.

SECURITY_EXTRACTION_VERSION = "sec-v1"
MAX_SECURITY_CLAIMS = 20
MAX_CLAIM_CHARS = 300

SECURITY_EXTRACTION_SYSTEM = (
    "You extract the security, compliance, incident, data-handling and privacy "
    "claims an AI assistant makes about one specific brand. Return strict JSON "
    "only, no prose. Follow the schema exactly."
)

SECURITY_EXTRACTION_TEMPLATE = """Target brand: "{brand}"
Keywords that identify the brand: {keywords}

Response to analyse:
---
{response}
---

Return JSON only, matching this schema exactly:
{{
  "claims": [                              // every statement about the TARGET brand's security, compliance,
                                           // incidents, data handling or privacy. Skip statements about other brands.
    {{"claim": str,                        // the statement in one sentence (max 300 chars), keeping the response's wording
      "kind": "certification" | "incident" | "encryption" | "access_control" | "privacy" | "vulnerability" | "other",
      "polarity": "affirms" | "denies" | "uncertain",
                                           // affirms = stated as fact ("is SOC 2 certified", "had a breach in 2023")
                                           // denies = states the opposite ("has never had a breach")
                                           // uncertain = hedged ("may", "reportedly", "I could not verify")
      "cited_url": str or null}}           // a URL the response cites for THIS claim, else null
  ],
  "fud_language": bool,                    // fear, uncertainty or doubt framing about the target
                                           // ("risky", "be careful", "cannot be trusted")
  "recommends_against": bool               // advises against using the target on security or privacy grounds
}}"""


def _normalise_security(raw: dict) -> dict:
    """Coerce a security-extraction payload into the persisted shape."""
    from apps.llm_ranking.services.security_lexicon import (
        CLAIM_KINDS,
        KIND_OTHER,
        POLARITIES,
        POLARITY_UNCERTAIN,
    )

    claims = []
    for c in (raw.get("claims") or []):
        if len(claims) >= MAX_SECURITY_CLAIMS:
            break
        if not isinstance(c, dict):
            continue
        text = (c.get("claim") or "").strip()
        if not text:
            continue
        kind = c.get("kind")
        if kind not in CLAIM_KINDS:
            kind = KIND_OTHER
        polarity = c.get("polarity")
        if polarity not in POLARITIES:
            polarity = POLARITY_UNCERTAIN
        url = c.get("cited_url")
        url = str(url).strip()[:500] if url else ""
        if url and not url.startswith(("http://", "https://")):
            url = ""
        claims.append({
            "claim": text[:MAX_CLAIM_CHARS],
            "kind": kind,
            "polarity": polarity,
            "cited_url": url,
        })
    return {
        "claims": claims,
        "fud_language": bool(raw.get("fud_language")),
        "recommends_against": bool(raw.get("recommends_against")),
    }


_SENTENCE_SPLIT = re.compile(r"(?:\r?\n)+|(?<=[.!?])\s+")


def _heuristic_security(response_text: str, brand_name: str, keywords: list) -> dict:
    """Regex fallback when the Haiku call fails.

    Every sentence that names the brand and touches a security topic
    becomes one claim, classified by the shared lexicon. Deliberately
    conservative: a sentence that names no brand term is ignored even if
    it is about security, because it may describe a competitor.
    """
    from apps.llm_ranking.services.security_lexicon import (
        AVOID_RE,
        FUD_RE,
        SECURITY_TOPIC_RE,
        kind_for,
        polarity_for,
    )

    terms = [t for t in [brand_name, *(keywords or [])] if t and str(t).strip()]
    patterns = [
        re.compile(r"(?<!\w)" + re.escape(str(t).strip()) + r"(?!\w)", re.IGNORECASE)
        for t in terms
    ]
    claims: list[dict] = []
    fud = False
    against = False
    for sentence in _SENTENCE_SPLIT.split(response_text or ""):
        unit = sentence.strip()
        if len(unit) < 12 or not any(p.search(unit) for p in patterns):
            continue
        if FUD_RE.search(unit):
            fud = True
        if AVOID_RE.search(unit):
            against = True
        if not SECURITY_TOPIC_RE.search(unit):
            continue
        claims.append({
            "claim": unit[:MAX_CLAIM_CHARS],
            "kind": kind_for(unit),
            "polarity": polarity_for(unit),
            "cited_url": "",
        })
        if len(claims) >= MAX_SECURITY_CLAIMS:
            break
    return {"claims": claims, "fud_language": fud, "recommends_against": against}


class SecurityClaimExtractionService:
    """Turn a raw security-probe answer into structured security claims.

    Always returns the same dict shape; on a failed model call it falls
    back to the lexicon heuristic and labels the row accordingly, so the
    perception aggregator can report how much of its data is heuristic.
    """

    MODEL = EXTRACTION_MODEL
    VERSION = SECURITY_EXTRACTION_VERSION

    @classmethod
    def extract(
        cls,
        *,
        response_text: str,
        brand_name: str,
        keywords: list,
        user=None,
        website=None,
        audit_id=None,
        idempotency_key=None,
    ) -> dict:
        """Returns ``{version, model, claims, fud_language, recommends_against}``."""
        if not response_text or not brand_name:
            return cls._empty()

        prompt = SECURITY_EXTRACTION_TEMPLATE.format(
            brand=brand_name,
            keywords=json.dumps(list(keywords or [])),
            response=response_text[:6000],
        )
        try:
            result = ClaudeUtility(model=cls.MODEL, max_tokens=1024).query(
                prompt,
                system_prompt=SECURITY_EXTRACTION_SYSTEM,
                user=user,
                website=website,
                audit_id=audit_id,
                role="security_extraction",
                module="llm_ranking",
                idempotency_key=idempotency_key,
            )
            if not result.succeeded:
                raise ValueError(f"security extraction call failed: {result.error}")
            parsed = _normalise_security(_parse_json_object(result.text))
            parsed["model"] = cls.MODEL
        except Exception as exc:
            logger.warning("Security extraction failed; falling back to heuristic: %s", exc)
            parsed = _heuristic_security(response_text, brand_name, keywords)
            parsed["model"] = "heuristic"
        parsed["version"] = cls.VERSION
        return parsed

    @staticmethod
    def _empty() -> dict:
        return {
            "version": SECURITY_EXTRACTION_VERSION,
            "model": "",
            "claims": [],
            "fud_language": False,
            "recommends_against": False,
        }
