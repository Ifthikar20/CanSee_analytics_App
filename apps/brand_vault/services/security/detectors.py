"""Detector registry for Brand Security response auditing.

Single source of truth for the trigger points the response auditor runs
against stored ``LLMRankingResult`` rows. Each detector has a stable code
(``BS-SENT-001`` style) that identifies the *type* of analysis that fired
— the code appears on every alert, in the API taxonomy, and in the UI, so
codes are never renumbered or reused.

Detection here is heuristic and offset-preserving: every finding records
character spans into its snippet so the UI can highlight exactly which
part of the answer triggered it. Nuanced detectors additionally declare a
``judge_mode`` so the response auditor can escalate them to the LLM judge:

* ``never``   — the heuristic verdict stands on its own.
* ``confirm`` — heuristic proposes; the judge may drop or re-grade it.
  Without a judge (disabled, no key, cost cap) the heuristic finding is
  kept at its default severity.
* ``require`` — the finding only exists if the judge (grounded in the
  brand's RAG material) confirms it. Without a judge it is skipped.

The registry deliberately imports FROM ``apps.brand_vault.models`` and
never the reverse; a registry-invariant test asserts every detector issue
is a valid model choice so the two cannot drift.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field

from apps.brand_vault.models import SafetyAlert
from apps.llm_ranking.services.security_lexicon import (
    AVOID_RE,
    CERTIFICATION_RE,
    FUD_RE,
    INCIDENT_RE,
    KIND_CERTIFICATION,
    KIND_INCIDENT,
    POLARITY_AFFIRMS,
    POLARITY_UNCERTAIN,
    SECURITY_TOPIC_RE,
    SUPPORT_SUPPORTED,
)

logger = logging.getLogger("apps")

# ── Severity ordering (used to prioritise findings before the cap) ──────

_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}

MAX_ALERTS_PER_RESULT = 5
MAX_SPANS_PER_ALERT = 10
MAX_SPAN_TEXT_CHARS = 300
_SNIPPET_RADIUS = 220
_MAX_UNIT_CHARS = 400


# ── Data shapes ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Span:
    """One highlighted region. Offsets are Unicode-codepoint indices into
    the finding's snippet exactly as persisted; ``text`` echoes the slice
    so a renderer can verify and re-anchor if the string ever shifts."""

    start: int
    end: int
    text: str
    label: str

    def as_dict(self) -> dict:
        return {
            "start": self.start,
            "end": self.end,
            "text": self.text[:MAX_SPAN_TEXT_CHARS],
            "label": self.label,
        }


@dataclass(frozen=True)
class Detector:
    code: str              # "BS-SENT-001" — stable forever
    slug: str              # "negative_sentiment"
    display_name: str
    category: str          # key into CATEGORIES
    issue: str             # a SafetyAlert.ISSUE_* value
    default_severity: str
    description: str
    recommended_action: str
    judge_mode: str = "never"      # never | confirm | require
    judge_question: str = ""
    # Issue codes a "require" judge may return for this detector. Empty
    # means the factual-misrepresentation trio (hallucination, unverified,
    # outdated), the historical default.
    judge_issues: tuple[str, ...] = ()


@dataclass
class DetectedFinding:
    detector: Detector
    issue: str
    severity: str
    title: str
    detail: str
    snippet: str
    spans: list[Span] = field(default_factory=list)
    sentiment_score: float | None = None


@dataclass
class DetectionContext:
    result: object                 # LLMRankingResult
    text: str                      # result.response_text
    brand: str                     # display name for copy
    brand_terms: list[str]         # all names that count as "the brand"
    competitors: list[dict]        # [{name, position, sentiment}, ...]


# ── Offset-preserving text helpers ──────────────────────────────────────

# LLM answers are markdown: tables, bullets and headings carry no sentence
# punctuation, so splitting on [.!?] alone can return an entire table as
# one "sentence" — which is how a competitor's feature ends up attributed
# to your brand. Split on line breaks and table pipes first, then sentence
# boundaries. (Offset-preserving port of the old response_auditor split.)
_UNIT_SPLIT = re.compile(r"(?:\r?\n)+|(?<=[.!?])\s+|\s*\|\s*")


def iter_units(text: str):
    """Yield ``(start, end)`` bounds of each markdown/sentence unit."""
    pos = 0
    for sep in _UNIT_SPLIT.finditer(text):
        if sep.start() > pos:
            yield pos, sep.start()
        pos = sep.end()
    if pos < len(text):
        yield pos, len(text)


def _term_pattern(term: str) -> re.Pattern:
    return re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)", re.IGNORECASE)


def unit_containing_any(text: str, terms: list[str]) -> tuple[int, int] | None:
    """Bounds of the smallest unit mentioning any term.

    The tightest unit matters: risk judgements are made against this
    window, and an over-wide unit imports words that belong to a
    different brand. Units are capped so a wall of text cannot smuggle
    unrelated phrases into the judgement window.
    """
    patterns = [_term_pattern(t) for t in terms if t]
    best: tuple[int, int] | None = None
    for start, end in iter_units(text):
        unit = text[start:end]
        if any(p.search(unit) for p in patterns):
            if best is None or (end - start) < (best[1] - best[0]):
                best = (start, min(end, start + _MAX_UNIT_CHARS))
    return best


def find_term_spans(
    text: str, terms: list[str], label: str,
    lo: int = 0, hi: int | None = None, limit: int = MAX_SPANS_PER_ALERT,
) -> list[Span]:
    """Whole-word occurrences of any term inside ``text[lo:hi]``."""
    hi = len(text) if hi is None else hi
    spans: list[Span] = []
    for term in terms:
        if not term:
            continue
        for m in _term_pattern(term).finditer(text, lo, hi):
            spans.append(Span(m.start(), m.end(), m.group(0), label))
            if len(spans) >= limit:
                return spans
    return spans


def find_pattern_spans(
    text: str, pattern: str | re.Pattern, label: str,
    lo: int = 0, hi: int | None = None, limit: int = MAX_SPANS_PER_ALERT,
) -> list[Span]:
    hi = len(text) if hi is None else hi
    regex = re.compile(pattern, re.IGNORECASE) if isinstance(pattern, str) else pattern
    spans: list[Span] = []
    for m in regex.finditer(text, lo, hi):
        spans.append(Span(m.start(), m.end(), m.group(0), label))
        if len(spans) >= limit:
            break
    return spans


def build_snippet(
    text: str, anchor_lo: int, anchor_hi: int, spans: list[Span],
    radius: int = _SNIPPET_RADIUS,
) -> tuple[str, list[Span]]:
    """Cut a context window around the anchor and rebase spans into it.

    Returns ``(snippet, rebased_spans)`` where every span offset indexes
    into the returned snippet string (ellipsis prefix included), so the
    stored spans stay valid against the stored snippet verbatim.
    """
    lo = max(0, anchor_lo - radius)
    hi = min(len(text), anchor_hi + radius)
    # Trim whitespace by moving the bounds, never by str.strip, so every
    # offset stays derivable from the original text.
    while lo < hi and text[lo].isspace():
        lo += 1
    while hi > lo and text[hi - 1].isspace():
        hi -= 1
    prefix = "…" if lo > 0 else ""
    suffix = "…" if hi < len(text) else ""
    snippet = f"{prefix}{text[lo:hi]}{suffix}"
    shift = len(prefix) - lo
    rebased = [
        Span(s.start + shift, s.end + shift, s.text, s.label)
        for s in spans
        if s.start >= lo and s.end <= hi
    ][:MAX_SPANS_PER_ALERT]
    return snippet, rebased


def compute_dedupe_key(detector_code: str, result) -> str:
    """Recurrence grouping key: same detector + prompt stream + provider.

    Keyed on ``source_prompt_id`` when the FK exists (survives prompt
    re-wording per region), else a hash of the normalized prompt text.
    Duplicated (intentionally) in migration 0010 so migrations never
    import application code.
    """
    if getattr(result, "source_prompt_id", None):
        prompt_key = str(result.source_prompt_id)
    else:
        normalized = " ".join((result.prompt or "").split()).lower()
        prompt_key = hashlib.sha256(normalized.encode()).hexdigest()
    raw = f"{detector_code}|{prompt_key}|{result.provider or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()[:40]


# ── Shared lexicons ─────────────────────────────────────────────────────

# Phrases that flip an otherwise neutral mention into a negative signal.
# Kept narrow on purpose — broad sentiment lists produce noise, and the
# extractor already records a per-answer sentiment we trust more.
_RISK_PATTERNS = [
    (r"\b(?:avoid|steer clear of|stay away from)\b", "explicit avoidance advice"),
    (r"\b(?:is|was|being)\s+a\s+(?:scam|ripoff|rip-off)\b", "fraud accusation"),
    (r"\b(?:fraudulent|scammed|defrauded)\b", "fraud accusation"),
    (r"\b(?:lawsuit|sued|class action|under investigation)\b", "legal exposure"),
    (r"\b(?:data breach|was hacked|security incident|prolonged outage)\b", "security or reliability incident"),
    (r"\b(?:hidden fees|overpriced)\b", "pricing objection"),
    (r"\b(?:poor|bad|terrible|awful|worst)\s+(?:support|service|experience|ux)\b", "service complaint"),
    (r"\b(?:deprecated|discontinued|shutting down|no longer supported)\b", "continuity doubt"),
]

# Phrases where a risk word is a product category, not an accusation. In
# fintech "fraud tools" and "fraud prevention" are features a vendor
# sells; flagging them as reputational risk is the fastest way to make
# this tool untrustworthy. Checked before any risk/derogatory rule fires.
_BENIGN_CONTEXT = re.compile(
    r"\b(?:anti-?fraud|fraud (?:tools?|prevention|detection|protection|management|monitoring|screening))\b"
    r"|\bchargeback\b|\brisk (?:tools?|engine|management)\b"
    r"|\b(?:breach|outage) (?:prevention|protection|detection)\b",
    re.IGNORECASE,
)

# Insulting tone, distinct from the factual risk claims above. These are
# words a support doc would never use about its own subject.
_DEROGATORY = re.compile(
    r"\b(?:scammy|shady|sketchy|dodgy|garbage|trash|useless|pathetic|"
    r"incompetent|dishonest|shoddy|laughable|amateurish|overhyped|junk)\b"
    r"|\brip[- ]?offs?\b"
    r"|\b(?:is|are)\s+(?:a\s+)?(?:joke|mess|disaster)s?\b",
    re.IGNORECASE,
)

# Hedged, lukewarm endorsement — the shape of "tone is not very positive"
# without being negative.
_HEDGE = re.compile(
    r"\b(?:might be worth|could consider|worth considering|worth a look|"
    r"an option|one option|one of many|one of several|lesser[- ]known|"
    r"not bad|may work|might work|if you prefer|for some users|"
    r"a decent|decent enough|fairly basic|somewhat limited)\b",
    re.IGNORECASE,
)

# Legitimacy doubt and caution advice.
_DISTRUST = re.compile(
    r"\b(?:proceed with caution|exercise caution|be cautious|"
    r"do your own research|hard to verify|difficult to verify|"
    r"cannot confirm|can't confirm|unable to verify|unverified claims|"
    r"mixed reviews|limited track record|credibility concerns|"
    r"red flags?|trust issues|questionable legitimacy|"
    r"legitimacy is unclear|whether it is legitimate)\b",
    re.IGNORECASE,
)

# Specific factual claim markers: currency, percentages, years, and
# company-fact verbs — the statements that can actually be wrong.
_CLAIM_MARKERS = re.compile(
    r"(?:\$|€|£)\s?\d"
    r"|\b\d+(?:\.\d+)?\s?%"
    r"|\b(?:19|20)\d{2}\b"
    r"|\b(?:founded|headquartered|based in|owned by|acquired(?: by)?|"
    r"charges|costs?|pricing|price|free plan|free tier|employees|"
    r"customers|subscription)\b",
    re.IGNORECASE,
)


# ── Detector implementations ────────────────────────────────────────────

def _brand_window(ctx: DetectionContext) -> tuple[int, int] | None:
    """The unit the brand is named in, else a window at the first term hit."""
    unit = unit_containing_any(ctx.text, ctx.brand_terms)
    if unit:
        return unit
    hits = find_term_spans(ctx.text, ctx.brand_terms, "Brand mention", limit=1)
    if hits:
        return hits[0].start, hits[0].end
    return None


def _detect_negative_sentiment(ctx: DetectionContext, det: Detector) -> list[DetectedFinding]:
    if not (ctx.result.is_mentioned and ctx.result.sentiment == "negative"):
        return []
    window = _brand_window(ctx)
    lo, hi = window if window else (0, min(len(ctx.text), _SNIPPET_RADIUS * 2))
    spans = find_term_spans(ctx.text, ctx.brand_terms, "Brand mention", lo, hi, limit=3)
    if not _BENIGN_CONTEXT.search(ctx.text[lo:hi]):
        for pattern, label in _RISK_PATTERNS:
            spans += find_pattern_spans(ctx.text, pattern, label, lo, hi, limit=2)
    snippet, rebased = build_snippet(ctx.text, lo, hi, spans)
    return [DetectedFinding(
        detector=det, issue=det.issue, severity=det.default_severity,
        title=f"{ctx.brand} described negatively",
        detail=(
            f"This answer mentions {ctx.brand} but the extracted sentiment "
            f"is negative. A mention that reads badly is worse than no "
            f"mention — it is the answer a buyer acts on."
        ),
        snippet=snippet, spans=rebased, sentiment_score=-0.7,
    )]


def _detect_weak_endorsement(ctx: DetectionContext, det: Detector) -> list[DetectedFinding]:
    if not (ctx.result.is_mentioned and ctx.result.sentiment == "neutral"):
        return []
    window = _brand_window(ctx)
    if not window:
        return []
    lo, hi = window
    hedges = find_pattern_spans(ctx.text, _HEDGE, "Hedged endorsement", lo, hi, limit=4)
    rec = (ctx.result.primary_recommendation or "").strip()
    rec_is_other = bool(rec) and not any(
        t.lower() in rec.lower() for t in ctx.brand_terms if t
    )
    if not (hedges or ctx.competitors or rec_is_other):
        return []
    spans = hedges or find_term_spans(
        ctx.text, ctx.brand_terms, "Brand mention", lo, hi, limit=3,
    )

    # Concrete copy beats abstractions: name who else is in the answer and
    # where the brand sits, so the reader sees the picture without opening
    # the full response.
    names = [
        (c.get("name") or "").strip()
        for c in ctx.competitors
        if (c.get("name") or "").strip()
    ]
    total = len(names) + 1
    rank = ctx.result.mention_rank

    if names:
        title = f"{ctx.brand} listed as one of {total} options"
    else:
        title = f"{ctx.brand} mentioned without a recommendation"

    sentences = []
    lead = f"This answer includes {ctx.brand} in a neutral tone, with no reason given to choose it"
    if names:
        shown = ", ".join(names[:3])
        more = len(names) - 3
        lead += f" — alongside {shown}" + (f" and {more} more" if more > 0 else "")
    sentences.append(lead + ".")
    if isinstance(rank, int) and names:
        sentences.append(f"{ctx.brand} appears at position {rank} of {total}.")
    if hedges:
        sentences.append(f'The wording is hedged ("{hedges[0].text}").')
    if rec_is_other:
        sentences.append(f"The answer's top recommendation is {rec}.")
    sentences.append("Buyers reading this act on whichever option the answer favors.")

    snippet, rebased = build_snippet(ctx.text, lo, hi, spans)
    return [DetectedFinding(
        detector=det, issue=det.issue, severity=det.default_severity,
        title=title,
        detail=" ".join(sentences),
        snippet=snippet, spans=rebased, sentiment_score=0.0,
    )]


def _detect_derogatory(ctx: DetectionContext, det: Detector) -> list[DetectedFinding]:
    window = unit_containing_any(ctx.text, ctx.brand_terms)
    if not window:
        return []
    lo, hi = window
    if _BENIGN_CONTEXT.search(ctx.text[lo:hi]):
        return []
    matches = find_pattern_spans(ctx.text, _DEROGATORY, "Derogatory language", lo, hi, limit=5)
    if not matches:
        return []
    spans = matches + find_term_spans(ctx.text, ctx.brand_terms, "Brand mention", lo, hi, limit=2)
    snippet, rebased = build_snippet(ctx.text, lo, hi, spans)
    words = ", ".join(sorted({m.text.lower() for m in matches}))
    return [DetectedFinding(
        detector=det, issue=det.issue, severity=det.default_severity,
        title=f"Derogatory language about {ctx.brand}: {words}",
        detail=(
            f"The sentence naming {ctx.brand} uses insulting language "
            f"({words}) rather than neutral factual criticism. Tone like "
            f"this shapes how every reader of the answer perceives the brand."
        ),
        snippet=snippet, spans=rebased, sentiment_score=-0.8,
    )]


def _detect_harmful(ctx: DetectionContext, det: Detector) -> list[DetectedFinding]:
    if not ctx.result.is_mentioned:
        return []
    window = unit_containing_any(ctx.text, ctx.brand_terms)
    if not window:
        return []
    lo, hi = window
    if _BENIGN_CONTEXT.search(ctx.text[lo:hi]):
        return []
    spans: list[Span] = []
    labels: list[str] = []
    for pattern, label in _RISK_PATTERNS:
        hits = find_pattern_spans(ctx.text, pattern, label, lo, hi, limit=2)
        if hits:
            spans += hits
            labels.append(label)
    if not spans:
        return []
    snippet, rebased = build_snippet(ctx.text, lo, hi, spans)
    return [DetectedFinding(
        detector=det, issue=det.issue, severity=det.default_severity,
        title=f"Risk language near {ctx.brand}: {labels[0]}",
        detail=(
            f"The sentence naming {ctx.brand} contains {', and '.join(labels)}. "
            f"Verify whether the claim is accurate; if it is not, this is a "
            f"correction to push into your brand facts."
        ),
        snippet=snippet, spans=rebased, sentiment_score=-0.7,
    )]


def _detect_unfavorable_comparison(ctx: DetectionContext, det: Detector) -> list[DetectedFinding]:
    if not ctx.result.is_mentioned:
        return []
    triggers: list[str] = []
    winner = ""

    rank = ctx.result.mention_rank
    for comp in ctx.competitors:
        pos = comp.get("position")
        if (
            comp.get("sentiment") == "positive"
            and isinstance(pos, int)
            and isinstance(rank, int)
            and pos < rank
        ):
            winner = comp.get("name") or ""
            triggers.append("ranked below a positively-described competitor")
            break

    rec = (ctx.result.primary_recommendation or "").strip()
    if rec and not any(t.lower() in rec.lower() for t in ctx.brand_terms if t):
        winner = winner or rec
        triggers.append("the answer's primary recommendation is someone else")

    brand_alt = "|".join(re.escape(t) for t in ctx.brand_terms if t)
    comparative = re.compile(
        rf"\b(?:better|superior|preferable|stronger|cheaper|faster)\s+than\s+(?:{brand_alt})\b"
        rf"|\bunlike\s+(?:{brand_alt})\b"
        rf"|\b(?:over|instead of|rather than)\s+(?:{brand_alt})\b",
        re.IGNORECASE,
    ) if brand_alt else None
    phrase_spans = (
        find_pattern_spans(ctx.text, comparative, "Unfavorable comparison", limit=3)
        if comparative else []
    )
    if phrase_spans:
        triggers.append("explicit comparative wording against the brand")

    if not triggers:
        return []

    spans = list(phrase_spans)
    if winner:
        spans += find_term_spans(ctx.text, [winner], "Favored competitor", limit=2)
    if spans:
        anchor_lo = min(s.start for s in spans)
        anchor_hi = max(s.end for s in spans)
    else:
        window = _brand_window(ctx) or (0, min(len(ctx.text), _SNIPPET_RADIUS * 2))
        anchor_lo, anchor_hi = window
    severity = "high" if len(triggers) >= 2 else det.default_severity
    snippet, rebased = build_snippet(ctx.text, anchor_lo, anchor_hi, spans)
    who = winner or "A competitor"
    return [DetectedFinding(
        detector=det, issue=det.issue, severity=severity,
        title=f"{who} favored over {ctx.brand}",
        detail=(
            f"This answer favors a competitor: {'; '.join(triggers)}. "
            f"The buyer reading it is being steered away from {ctx.brand}."
        ),
        snippet=snippet, spans=rebased, sentiment_score=-0.3,
    )]


def _detect_factual_claims(ctx: DetectionContext, det: Detector) -> list[DetectedFinding]:
    """Proposal only — judge_mode 'require' means the response auditor
    keeps this finding only when the RAG-grounded judge confirms it."""
    if not ctx.result.is_mentioned:
        return []
    window = unit_containing_any(ctx.text, ctx.brand_terms)
    if not window:
        return []
    lo, hi = window
    markers = find_pattern_spans(ctx.text, _CLAIM_MARKERS, "Factual claim", lo, hi, limit=5)
    if not markers:
        return []
    spans = markers + find_term_spans(ctx.text, ctx.brand_terms, "Brand mention", lo, hi, limit=2)
    snippet, rebased = build_snippet(ctx.text, lo, hi, spans)
    return [DetectedFinding(
        detector=det, issue=det.issue, severity=det.default_severity,
        title=f"Specific factual claims about {ctx.brand}",
        detail=(
            f"This answer states specific facts about {ctx.brand} (figures, "
            f"dates, pricing or ownership). Checked against your Brand Input "
            f"material to confirm whether the claims hold."
        ),
        snippet=snippet, spans=rebased, sentiment_score=None,
    )]


def _detect_impersonation(ctx: DetectionContext, det: Detector) -> list[DetectedFinding]:
    if ctx.result.is_mentioned:
        return []
    spans = find_term_spans(ctx.text, ctx.brand_terms, "Brand name", limit=3)
    if not spans:
        return []
    snippet, rebased = build_snippet(ctx.text, spans[0].start, spans[0].end, spans)
    return [DetectedFinding(
        detector=det, issue=det.issue, severity=det.default_severity,
        title=f"{ctx.brand} appears without being recognised as a distinct brand",
        detail=(
            f"The answer text contains '{ctx.brand}' but the extractor did "
            f"not record it as a mention. That usually means the name "
            f"appeared inside another brand's description or as a generic "
            f"term — your identity is being folded into someone else's."
        ),
        snippet=snippet, spans=rebased, sentiment_score=None,
    )]


# ── Private / non-public data (PII, NPI, credentials) ───────────────────
#
# Two tiers, because the cost of being wrong differs:
#   SECRETS  — credentials and government/financial identifiers. A match
#              is a problem regardless of context.
#   CONTACT  — emails, phone numbers, postal addresses. These are only a
#              problem when the data is PERSONAL rather than the brand's
#              published business contact, so the judge adjudicates
#              (judge_mode="confirm") before the finding is kept.
_PRIVATE_SECRET_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "Private key"),
    (r"\bAKIA[0-9A-Z]{16}\b", "AWS access key"),
    (r"\bsk-[A-Za-z0-9_-]{20,}\b", "API secret key"),
    (r"\b(?:api[_-]?key|secret|access[_-]?token|bearer)\b\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}",
     "Credential"),
    (r"\b\d{3}-\d{2}-\d{4}\b", "Government ID (SSN format)"),
    (r"\b(?:4\d{3}|5[1-5]\d{2}|3[47]\d{2}|6011)[ -]?\d{4}[ -]?\d{4}[ -]?\d{2,4}\b",
     "Payment card number"),
    (r"\b(?:IBAN|routing(?:\s+number)?|account\s+number)\b\s*[:#]?\s*[A-Z0-9]{8,}",
     "Bank account detail"),
)
_PRIVATE_CONTACT_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b", "Email address"),
    (r"(?:\+?\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b", "Phone number"),
    (r"\b\d{1,5}\s+[A-Z][\w.]*(?:\s+[A-Z][\w.]*)*\s+"
     r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Way|Court|Ct)\b",
     "Postal address"),
    (r"\b(?:date\s+of\s+birth|DOB|home\s+address|personal\s+(?:email|phone|mobile))\b",
     "Personal identifier"),
)


def _detect_private_data(ctx: DetectionContext, det: Detector) -> list[DetectedFinding]:
    """Flag private / non-public data about the brand or its people
    surfacing inside an AI answer.

    Unlike the accuracy detectors, a finding here does NOT mean the model
    is wrong — a correct home address or live API key is worse than a
    wrong one. Only runs when the answer actually names the brand, so a
    generic answer that happens to contain a phone-shaped number does not
    raise an alert.
    """
    brand_hits = find_term_spans(ctx.text, ctx.brand_terms, "Brand mention", limit=2)
    if not brand_hits:
        return []

    secret_spans: list[Span] = []
    secret_labels: list[str] = []
    for pattern, label in _PRIVATE_SECRET_PATTERNS:
        hits = find_pattern_spans(ctx.text, pattern, label, limit=3)
        if hits:
            secret_spans += hits
            secret_labels.append(label)

    contact_spans: list[Span] = []
    contact_labels: list[str] = []
    for pattern, label in _PRIVATE_CONTACT_PATTERNS:
        hits = find_pattern_spans(ctx.text, pattern, label, limit=3)
        if hits:
            contact_spans += hits
            contact_labels.append(label)

    spans = secret_spans + contact_spans
    if not spans:
        return []

    kinds = secret_labels + contact_labels
    anchor = min(spans, key=lambda s: s.start)
    snippet, rebased = build_snippet(ctx.text, anchor.start, anchor.end, spans + brand_hits)

    # Lead with what was found, then why it matters — a credential leak
    # and a stray phone number deserve different urgency in the copy.
    if secret_labels:
        lead = (
            f"This answer contains what looks like credential or identifier "
            f"data ({', '.join(secret_labels)}) in a response naming {ctx.brand}."
        )
        why = (
            "Secrets echoed by an AI answer can be harvested at scale and "
            "suggest the model ingested material that was never meant to be "
            "public. Rotate anything real and trace where it was published."
        )
        severity = "high"
    else:
        lead = (
            f"This answer exposes contact or personal data "
            f"({', '.join(contact_labels)}) in a response naming {ctx.brand}."
        )
        why = (
            "If this is a person's direct line or private address rather "
            "than your published business contact, it is a privacy exposure "
            "for that individual and a compliance risk for you."
        )
        severity = det.default_severity

    quoted = ", ".join(f'"{s.text[:60]}"' for s in spans[:3])
    return [DetectedFinding(
        detector=det, issue=det.issue, severity=severity,
        title=f"Private data about {ctx.brand} in an AI answer: {kinds[0]}",
        detail=f"{lead} The answer includes {quoted}. {why}",
        snippet=snippet, spans=rebased, sentiment_score=None,
    )]


def _detect_distrust(ctx: DetectionContext, det: Detector) -> list[DetectedFinding]:
    window = unit_containing_any(ctx.text, ctx.brand_terms)
    if not window:
        return []
    lo, hi = window
    matches = find_pattern_spans(ctx.text, _DISTRUST, "Distrust signal", lo, hi, limit=5)
    if not matches:
        return []
    spans = matches + find_term_spans(ctx.text, ctx.brand_terms, "Brand mention", lo, hi, limit=2)
    snippet, rebased = build_snippet(ctx.text, lo, hi, spans)
    phrases = ", ".join(sorted({m.text.lower() for m in matches}))
    return [DetectedFinding(
        detector=det, issue=det.issue, severity=det.default_severity,
        title=f"Legitimacy doubt cast on {ctx.brand}",
        detail=(
            f"The answer hedges on whether {ctx.brand} can be trusted "
            f"({phrases}). Caution advice in an AI answer suppresses "
            f"conversion even when nothing negative is claimed outright."
        ),
        snippet=snippet, spans=rebased, sentiment_score=-0.4,
    )]


# ── Security perception ─────────────────────────────────────────────────
#
# These read the second extraction pass (``LLMRankingResult.security_claims``)
# when a security probe produced one, so the flagged text is the claim
# the model actually made. Answers without that pass (visibility audits,
# older rows) fall back to the shared lexicon inside the brand's sentence.

def _security_payload(ctx: DetectionContext) -> dict:
    payload = getattr(ctx.result, "security_claims", None)
    return payload if isinstance(payload, dict) else {}


def _has_extraction(ctx: DetectionContext) -> bool:
    """True when the security extraction pass ran for this answer. Its
    verdict then stands: the lexicon fallback exists for answers that
    never had one (visibility audits, rows older than the feature)."""
    return "claims" in _security_payload(ctx)


def _security_claims(ctx: DetectionContext, kind: str, polarities: tuple[str, ...]) -> list[dict]:
    return [
        c for c in (_security_payload(ctx).get("claims") or [])
        if isinstance(c, dict)
        and c.get("kind") == kind
        and c.get("polarity") in polarities
        # The brand's own material backs it: nothing to raise.
        and c.get("support") != SUPPORT_SUPPORTED
    ]


def _locate_claim(text: str, claim: str) -> tuple[int, int] | None:
    """Find the claim's wording in the answer: the whole claim first, then
    its opening 60 characters (extractors keep the opening words even when
    they trim the tail). None when the extractor paraphrased it away."""
    haystack = text.lower()
    for probe in (claim, claim[:60]):
        needle = (probe or "").strip().lower()
        if len(needle) < 12:
            continue
        idx = haystack.find(needle)
        if idx >= 0:
            return idx, idx + len(needle)
    return None


def _claim_spans(
    ctx: DetectionContext, claims: list[dict], label: str, limit: int = 5,
) -> tuple[list[Span], tuple[int, int] | None]:
    """Spans for extracted claims that can be located verbatim, plus the
    window bounding them. A finding never quotes text that is not there."""
    spans: list[Span] = []
    lo = hi = None
    for c in claims:
        found = _locate_claim(ctx.text, c.get("claim") or "")
        if not found:
            continue
        start, end = found
        spans.append(Span(start, end, ctx.text[start:end], label))
        lo = start if lo is None else min(lo, start)
        hi = end if hi is None else max(hi, end)
        if len(spans) >= limit:
            break
    if not spans:
        return [], None
    return spans, (lo, hi)


def _brand_unit_spans(
    ctx: DetectionContext, pattern: re.Pattern, label: str,
) -> tuple[list[Span], tuple[int, int] | None]:
    """Lexicon hits inside the sentence that names the brand."""
    if not ctx.result.is_mentioned:
        return [], None
    window = unit_containing_any(ctx.text, ctx.brand_terms)
    if not window:
        return [], None
    lo, hi = window
    if _BENIGN_CONTEXT.search(ctx.text[lo:hi]):
        return [], None
    spans = find_pattern_spans(ctx.text, pattern, label, lo, hi, limit=5)
    if not spans:
        return [], None
    return spans, window


def _detect_compliance_claim(ctx: DetectionContext, det: Detector) -> list[DetectedFinding]:
    """Proposal only (judge_mode 'require'): the RAG-grounded judge keeps
    it when the brand's material does not support the compliance claim."""
    if _has_extraction(ctx):
        claims = _security_claims(ctx, KIND_CERTIFICATION, (POLARITY_AFFIRMS,))
        spans, window = _claim_spans(ctx, claims, "Compliance claim") if claims else ([], None)
    else:
        spans, window = _brand_unit_spans(ctx, CERTIFICATION_RE, "Compliance claim")
    if not window:
        return []
    lo, hi = window
    spans = spans + find_term_spans(ctx.text, ctx.brand_terms, "Brand mention", lo, hi, limit=2)
    snippet, rebased = build_snippet(ctx.text, lo, hi, spans)
    return [DetectedFinding(
        detector=det, issue=det.issue, severity=det.default_severity,
        title=f"Compliance claim about {ctx.brand}",
        detail=(
            f"This answer states a certification, compliance status or security "
            f"control for {ctx.brand}. Checked against your Brand Input material: "
            f"a certification the model invents is one a buyer will later find "
            f"you cannot show."
        ),
        snippet=snippet, spans=rebased, sentiment_score=None,
    )]


def _detect_incident_claim(ctx: DetectionContext, det: Detector) -> list[DetectedFinding]:
    """Proposal only (judge_mode 'require'): kept when the ground truth
    does not confirm the breach, hack or vulnerability the answer describes."""
    severity = det.default_severity
    if _has_extraction(ctx):
        claims = _security_claims(ctx, KIND_INCIDENT, (POLARITY_AFFIRMS, POLARITY_UNCERTAIN))
        spans, window = _claim_spans(ctx, claims, "Incident claim") if claims else ([], None)
        if window and all(c.get("polarity") == POLARITY_UNCERTAIN for c in claims):
            severity = "medium"  # hedged ("reportedly") is damaging, not damning
    else:
        spans, window = _brand_unit_spans(ctx, INCIDENT_RE, "Incident claim")
    if not window:
        return []
    lo, hi = window
    spans = spans + find_term_spans(ctx.text, ctx.brand_terms, "Brand mention", lo, hi, limit=2)
    snippet, rebased = build_snippet(ctx.text, lo, hi, spans)
    return [DetectedFinding(
        detector=det, issue=det.issue, severity=severity,
        title=f"Security incident attributed to {ctx.brand}",
        detail=(
            f"This answer attributes a breach, hack, leak or vulnerability to "
            f"{ctx.brand}. Checked against your Brand Input material: an incident "
            f"that never happened, or one long since resolved, keeps costing "
            f"deals for as long as models repeat it."
        ),
        snippet=snippet, spans=rebased, sentiment_score=-0.6,
    )]


def _detect_security_advisory(ctx: DetectionContext, det: Detector) -> list[DetectedFinding]:
    payload = _security_payload(ctx)
    if _has_extraction(ctx) and not payload.get("recommends_against"):
        return []
    extracted = bool(payload.get("recommends_against"))
    if extracted:
        spans = (
            find_pattern_spans(ctx.text, AVOID_RE, "Advice against", limit=3)
            + find_pattern_spans(ctx.text, FUD_RE, "Fear framing", limit=3)
        )
        if spans:
            lo = min(s.start for s in spans)
            hi = max(s.end for s in spans)
        else:
            window = _brand_window(ctx)
            if not window:
                return []
            lo, hi = window
        severity = "high"
    else:
        if not ctx.result.is_mentioned:
            return []
        window = unit_containing_any(ctx.text, ctx.brand_terms)
        if not window:
            return []
        lo, hi = window
        unit = ctx.text[lo:hi]
        if _BENIGN_CONTEXT.search(unit) or not AVOID_RE.search(unit):
            return []
        if not (SECURITY_TOPIC_RE.search(unit) or FUD_RE.search(unit)):
            return []
        spans = (
            find_pattern_spans(ctx.text, AVOID_RE, "Advice against", lo, hi, limit=2)
            + find_pattern_spans(ctx.text, FUD_RE, "Fear framing", lo, hi, limit=2)
        )
        severity = det.default_severity
    spans = spans + find_term_spans(ctx.text, ctx.brand_terms, "Brand mention", lo, hi, limit=2)
    snippet, rebased = build_snippet(ctx.text, lo, hi, spans)
    return [DetectedFinding(
        detector=det, issue=det.issue, severity=severity,
        title=f"{ctx.brand} advised against on security grounds",
        detail=(
            f"This answer steers the reader away from {ctx.brand} for security or "
            f"privacy reasons. A security-conscious buyer reading it will not "
            f"shortlist you."
        ),
        snippet=snippet, spans=rebased, sentiment_score=-0.8,
    )]


# ── Registry ────────────────────────────────────────────────────────────

CATEGORIES: tuple[tuple[str, str], ...] = (
    ("sentiment", "Sentiment"),
    ("language", "Language"),
    ("association", "Association"),
    ("competitive", "Competitive"),
    ("accuracy", "Accuracy"),
    ("identity", "Identity"),
    ("trust", "Trust"),
    ("privacy", "Privacy"),
    ("security", "Security"),
)

DETECTORS: tuple[Detector, ...] = (
    Detector(
        code="BS-SENT-001", slug="negative_sentiment",
        display_name="Negative sentiment", category="sentiment",
        issue=SafetyAlert.ISSUE_NEGATIVE, default_severity="high",
        description=(
            "The brand is mentioned and the answer's extracted sentiment "
            "toward it is negative."
        ),
        recommended_action=(
            "Review the flagged response and publish corrective positioning "
            "content; update your approved brand facts so future answers "
            "cite them."
        ),
    ),
    Detector(
        code="BS-SENT-002", slug="weak_endorsement",
        display_name="Weak endorsement", category="sentiment",
        issue=SafetyAlert.ISSUE_WEAK_ENDORSEMENT, default_severity="low",
        description=(
            "The brand is mentioned with neutral, hedged wording — listed "
            "as one option among many rather than recommended."
        ),
        recommended_action=(
            "Strengthen the proof points for this prompt theme: case "
            "studies, comparisons and differentiators in crawlable pages "
            "give answers a reason to endorse you."
        ),
    ),
    Detector(
        code="BS-LANG-001", slug="derogatory_language",
        display_name="Derogatory language", category="language",
        issue=SafetyAlert.ISSUE_DEROGATORY, default_severity="high",
        judge_mode="confirm",
        judge_question=(
            "Does this answer use derogatory or insulting language about "
            "the brand, as opposed to neutral factual criticism?"
        ),
        description=(
            "Insulting or mocking language in the sentence that names the "
            "brand — tone damage, distinct from factual risk claims."
        ),
        recommended_action=(
            "Trace where the hostile framing originates (reviews, forums, "
            "press) and address it at the source; publish authoritative "
            "content that displaces the framing."
        ),
    ),
    Detector(
        code="BS-HARM-001", slug="harmful_association",
        display_name="Harmful association", category="association",
        issue=SafetyAlert.ISSUE_HARMFUL, default_severity="high",
        description=(
            "Risk language — fraud accusations, lawsuits, breaches, "
            "avoidance advice — in the sentence naming the brand."
        ),
        recommended_action=(
            "Verify whether the claim is accurate; if false, add a "
            "correction to your brand facts and pursue cleanup with the "
            "pages the answer cites."
        ),
    ),
    Detector(
        code="BS-COMP-001", slug="unfavorable_comparison",
        display_name="Unfavorable comparison", category="competitive",
        issue=SafetyAlert.ISSUE_UNFAVORABLE_COMPARISON, default_severity="medium",
        description=(
            "A competitor is described positively above the brand, is the "
            "answer's primary recommendation, or is explicitly called "
            "better than the brand."
        ),
        recommended_action=(
            "Build or refresh comparison content for this prompt theme; "
            "ensure your differentiators are documented in citable pages "
            "the answer engines crawl."
        ),
    ),
    Detector(
        code="BS-FACT-001", slug="factual_misrepresentation",
        display_name="Factual misrepresentation", category="accuracy",
        issue=SafetyAlert.ISSUE_UNVERIFIED, default_severity="medium",
        judge_mode="require",
        judge_question=(
            "Do the factual claims this answer makes about the brand "
            "contradict the ground truth (hallucination), or are they "
            "absent from it (unverified)?"
        ),
        description=(
            "Specific factual claims about the brand — figures, dates, "
            "pricing, ownership — checked against the Brand Input ground "
            "truth. Only raised when the judge confirms a discrepancy."
        ),
        recommended_action=(
            "Correct the record: update your Brand Input material with the "
            "authoritative facts and publish them where answer engines can "
            "cite them."
        ),
    ),
    Detector(
        code="BS-IMP-001", slug="impersonation",
        display_name="Impersonation", category="identity",
        issue=SafetyAlert.ISSUE_IMPERSONATION, default_severity="medium",
        description=(
            "The brand's name appears in the answer without being "
            "recognised as a distinct brand — folded into another brand's "
            "description or used as a generic term."
        ),
        recommended_action=(
            "Check whether your name is being conflated with another "
            "brand; publish disambiguation content and correct third-party "
            "listings that blur the identity."
        ),
    ),
    Detector(
        code="BS-TRST-001", slug="distrust_signals",
        display_name="Distrust signals", category="trust",
        issue=SafetyAlert.ISSUE_DISTRUST, default_severity="medium",
        judge_mode="confirm",
        judge_question=(
            "Does this answer cast doubt on the brand's legitimacy or "
            "trustworthiness?"
        ),
        description=(
            "Caution advice or legitimacy doubt — 'proceed with caution', "
            "'hard to verify', 'mixed reviews' — attached to the brand."
        ),
        recommended_action=(
            "Shore up trust signals: reviews, security pages, company "
            "information and third-party validation that answer engines "
            "can verify."
        ),
    ),
    Detector(
        code="BS-PRIV-001", slug="private_data_exposure",
        display_name="Private data exposure", category="privacy",
        issue=SafetyAlert.ISSUE_PRIVATE_DATA, default_severity="medium",
        judge_mode="confirm",
        judge_question=(
            "Does this answer expose private, personal or non-public data "
            "(a personal email or direct phone line, home address, "
            "government or financial identifier, or a credential) as "
            "opposed to the brand's published business contact details?"
        ),
        description=(
            "Personal, non-public or credential data about the brand or "
            "its people appearing inside an AI answer. Unlike an accuracy "
            "issue, correct data is worse than wrong data here."
        ),
        recommended_action=(
            "Confirm whether the data is genuinely private; if it is, "
            "rotate any exposed credential, then find and remove the "
            "public page or dataset the model learned it from and request "
            "removal from caches and training sources."
        ),
    ),
    Detector(
        code="BS-SEC-001", slug="hallucinated_compliance",
        display_name="Unsupported compliance claim", category="security",
        issue=SafetyAlert.ISSUE_HALLUCINATED_COMPLIANCE, default_severity="high",
        judge_mode="require",
        judge_issues=(
            SafetyAlert.ISSUE_HALLUCINATED_COMPLIANCE,
            SafetyAlert.ISSUE_OUTDATED,
            SafetyAlert.ISSUE_UNVERIFIED,
        ),
        judge_question=(
            "Does this answer assert a certification, compliance status or "
            "security control for the brand that the ground truth contradicts "
            "or does not support (hallucinated_compliance), describes as no "
            "longer current (outdated), or does not mention at all (unverified)?"
        ),
        description=(
            "A certification, compliance status or security control stated "
            "for the brand (SOC 2, ISO 27001, HIPAA, encryption at rest) that "
            "the Brand Input ground truth does not back. Only raised when the "
            "judge confirms the discrepancy."
        ),
        recommended_action=(
            "Publish a trust page that lists exactly what you hold and what "
            "you do not, add it to Brand Input, and make it citable so answer "
            "engines stop inferring."
        ),
    ),
    Detector(
        code="BS-SEC-002", slug="unconfirmed_incident",
        display_name="Unconfirmed security incident", category="security",
        issue=SafetyAlert.ISSUE_FALSE_INCIDENT, default_severity="high",
        judge_mode="require",
        judge_issues=(
            SafetyAlert.ISSUE_FALSE_INCIDENT,
            SafetyAlert.ISSUE_OUTDATED,
            SafetyAlert.ISSUE_UNVERIFIED,
        ),
        judge_question=(
            "Does this answer attribute a data breach, hack, leak or "
            "vulnerability to the brand that the ground truth does not confirm "
            "(false_incident), that was resolved and is presented as current "
            "(outdated), or that the ground truth does not mention (unverified)?"
        ),
        description=(
            "A breach, hack, leak or vulnerability attributed to the brand "
            "that the Brand Input ground truth does not confirm. Only raised "
            "when the judge confirms the discrepancy."
        ),
        recommended_action=(
            "If the incident never happened, document that in Brand Input and "
            "trace the source the answer cites; if it did, publish the "
            "post-incident report and remediation so answers can say it was "
            "resolved."
        ),
    ),
    Detector(
        code="BS-SEC-003", slug="security_advisory",
        display_name="Advised against on security grounds", category="security",
        issue=SafetyAlert.ISSUE_SECURITY_ADVISORY, default_severity="medium",
        judge_mode="confirm",
        judge_question=(
            "Does this answer advise the reader against using the brand on "
            "security or privacy grounds, as opposed to a neutral description?"
        ),
        description=(
            "The answer steers the reader away from the brand for security or "
            "privacy reasons: avoidance advice, fear framing, or a competitor "
            "recommended as the safer choice."
        ),
        recommended_action=(
            "Find the source of the framing (a review, a forum thread, an old "
            "incident) and answer it with verifiable security documentation "
            "answer engines can cite."
        ),
    ),
)

DETECTOR_INDEX: dict[str, Detector] = {d.code: d for d in DETECTORS}

_DETECT_FN = {
    "BS-SENT-001": _detect_negative_sentiment,
    "BS-SENT-002": _detect_weak_endorsement,
    "BS-LANG-001": _detect_derogatory,
    "BS-HARM-001": _detect_harmful,
    "BS-COMP-001": _detect_unfavorable_comparison,
    "BS-FACT-001": _detect_factual_claims,
    "BS-IMP-001": _detect_impersonation,
    "BS-TRST-001": _detect_distrust,
    "BS-PRIV-001": _detect_private_data,
    "BS-SEC-001": _detect_compliance_claim,
    "BS-SEC-002": _detect_incident_claim,
    "BS-SEC-003": _detect_security_advisory,
}

# Legacy issue code -> detector code, for rows written before detector
# codes existed (and for dormant-agent rows, which only carry an issue).
ISSUE_FALLBACK: dict[str, str] = {
    SafetyAlert.ISSUE_NEGATIVE: "BS-SENT-001",
    SafetyAlert.ISSUE_SENTIMENT_DROP: "BS-SENT-002",
    SafetyAlert.ISSUE_WEAK_ENDORSEMENT: "BS-SENT-002",
    SafetyAlert.ISSUE_DEROGATORY: "BS-LANG-001",
    SafetyAlert.ISSUE_HARMFUL: "BS-HARM-001",
    SafetyAlert.ISSUE_UNFAVORABLE_COMPARISON: "BS-COMP-001",
    SafetyAlert.ISSUE_HALLUCINATION: "BS-FACT-001",
    SafetyAlert.ISSUE_UNVERIFIED: "BS-FACT-001",
    SafetyAlert.ISSUE_OUTDATED: "BS-FACT-001",
    SafetyAlert.ISSUE_IMPERSONATION: "BS-IMP-001",
    SafetyAlert.ISSUE_DISTRUST: "BS-TRST-001",
    SafetyAlert.ISSUE_PRIVATE_DATA: "BS-PRIV-001",
    SafetyAlert.ISSUE_HALLUCINATED_COMPLIANCE: "BS-SEC-001",
    SafetyAlert.ISSUE_FALSE_INCIDENT: "BS-SEC-002",
    SafetyAlert.ISSUE_SECURITY_ADVISORY: "BS-SEC-003",
}


def run_detectors(ctx: DetectionContext) -> list[DetectedFinding]:
    """Run every registered detector against one stored response.

    Returns findings ordered by severity (high first, registry order as
    tie-break) and capped at MAX_ALERTS_PER_RESULT so a single bad answer
    cannot flood the alert queue.
    """
    if not ctx.text or not ctx.brand_terms:
        return []
    findings: list[DetectedFinding] = []
    for detector in DETECTORS:
        fn = _DETECT_FN[detector.code]
        try:
            findings.extend(fn(ctx, detector))
        except Exception:  # pragma: no cover — one detector never sinks the scan
            logger.exception(
                "detector %s failed for result %s", detector.code, ctx.result.pk,
            )
    findings.sort(key=lambda f: _SEVERITY_RANK.get(f.severity, 3))
    return findings[:MAX_ALERTS_PER_RESULT]
