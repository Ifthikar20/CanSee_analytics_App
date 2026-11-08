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
        ``confidence_score`` is None: the extractor does not compute one.
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
            result["confidence_score"] = None
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
            "confidence_score": None,
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
