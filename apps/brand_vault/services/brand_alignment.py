"""Score an LLM answer against the brand's own knowledge.

The Brand Input page collects what the brand wants to be known for —
crawled pages, pasted copy, quick notes — as ``rag.KnowledgeChunk`` rows,
with curated ``BrandFact`` triples extracted from them. This module
benchmarks a stored ``LLMRankingResult`` answer against that material in
two directions, using embedding cosine similarity only (no LLM call, so
it can run on every response of every prompt run):

* support   — of the answer's brand-scoped statements, how many are
              backed by a fact or a knowledge chunk;
* coverage  — of the brand messages most relevant to the prompt, how
              many the answer actually reflects (and which are missing).

Composite score is 0-100 (matching the sentiment/confidence/overall
conventions). When scoring is not meaningful the result carries an
explicit status and a NULL score — never a fabricated midpoint, which
would poison audit means and the dashboard KPI:

* ``no_brand_input``          — no chunks and no approved/auto facts;
* ``no_brand_claims``         — the answer never talks about the brand
                                (that is a Visibility problem, not an
                                alignment one);
* ``embeddings_unavailable``  — hash-fallback vectors are non-semantic,
                                so similarity would be noise.

Contradiction detection deliberately stays with the Brand Security
BS-FACT-001 judge (``services/security/detectors.py``); this module only
measures alignment and never raises SafetyAlerts.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from apps.brand_vault.models import BrandFact, FactStatus
from apps.brand_vault.services.security.agents._helpers import brand_terms
from apps.brand_vault.services.security.detectors import iter_units
from apps.rag.services.embedder import (
    FALLBACK_MODEL,
    cosine_similarity,
    embed_texts,
)

logger = logging.getLogger("apps")

ALIGNMENT_VERSION = "v1"

# Reuse the only production-tuned cosine cutoff in the codebase
# (accuracy_guard.SIMILARITY_THRESHOLD). Separate constants so the two
# directions can diverge later without archaeology.
SUPPORT_THRESHOLD = 0.55
COVERAGE_THRESHOLD = 0.55
# Minimum cosine(fact, prompt) for a fact to count as "expected in this
# answer" — stops niche prompts from being penalized for missing
# irrelevant messages.
RELEVANCE_FLOOR = 0.30

MAX_UNITS = 40           # units embedded per response (one batch call)
MAX_BRAND_UNITS = 20     # brand-scoped units scored for support
COVERAGE_K = 5           # top prompt-relevant facts checked for presence
MAX_FACTS = 300          # comparator caps; corpus is typically < 1000
MAX_CHUNKS = 500
SUPPORT_WEIGHT = 0.6
COVERAGE_WEIGHT = 0.4

MAX_SAMPLE_TEXT = 200
MAX_FACT_TEXT = 120
MAX_SAMPLES = 10
MAX_COVERAGE_ITEMS = 5

STATUS_SCORED = "scored"
STATUS_NO_BRAND_INPUT = "no_brand_input"
STATUS_NO_BRAND_CLAIMS = "no_brand_claims"
STATUS_EMBEDDINGS_UNAVAILABLE = "embeddings_unavailable"

_EMPHASIS = re.compile(r"[*_`#>]+")
_SENTENCE_PUNCT = re.compile(r"[.!?]")


def _looks_like_heading(unit: str) -> bool:
    """Heading-shaped lines carry no verifiable claim: they end with a
    colon, or they are a handful of words with no sentence punctuation
    ("Key Features"). Longer punctuation-less lines (table cells, list
    items) are kept — they often hold the actual claims."""
    if unit.endswith(":"):
        return True
    return len(unit.split()) <= 4 and not _SENTENCE_PUNCT.search(unit)


@dataclass
class AlignmentResult:
    score: float | None
    status: str
    detail: dict = field(default_factory=dict)
    model: str = ""
    version: str = ALIGNMENT_VERSION


def _skip(status: str) -> AlignmentResult:
    return AlignmentResult(
        score=None, status=status,
        detail={"version": ALIGNMENT_VERSION, "status": status},
    )


def _clean_unit(text: str) -> str:
    return _EMPHASIS.sub("", text or "").strip()


def _extract_units(text: str, terms: list[str]) -> list[tuple[str, bool]]:
    """``(unit_text, is_brand_scoped)`` for the response's usable units.

    Reuses the security detectors' markdown-aware splitter so a table
    row can never smuggle a competitor's sentence into a brand claim.
    Questions and heading-shaped lines carry no verifiable claim and are
    dropped.
    """
    patterns = [
        re.compile(r"(?<!\w)" + re.escape(t) + r"(?!\w)", re.IGNORECASE)
        for t in terms if t
    ]
    units: list[tuple[str, bool]] = []
    for start, end in iter_units(text or ""):
        raw = text[start:end]
        unit = _clean_unit(raw)
        if len(unit) < 20 or len(unit) > 400:
            continue
        if unit.endswith("?"):
            continue
        if _looks_like_heading(unit):
            continue
        is_brand = any(p.search(unit) for p in patterns)
        units.append((unit, is_brand))
        if len(units) >= MAX_UNITS:
            break
    return units


def _fact_text(fact) -> str:
    return f"{fact.subject} {fact.predicate} {fact.object}"[:MAX_FACT_TEXT]


def _load_facts(website, topics=None) -> list[tuple[BrandFact, list[float]]]:
    """Approved/auto facts with embeddings. ``topics`` (an iterable of
    ``BrandFact.topic`` values) narrows the comparator set, e.g. to the
    security and privacy facts the perception scorer checks against."""
    qs = BrandFact.objects.filter(
        website=website,
        status__in=(FactStatus.APPROVED, FactStatus.AUTO),
        version_to__isnull=True,
    )
    if topics:
        qs = qs.filter(topic__in=list(topics))
    rows = qs.only("id", "subject", "predicate", "object", "embedding")[:MAX_FACTS]
    return [(f, f.embedding) for f in rows if f.embedding]


def _load_chunks(user, website) -> list[tuple[str, list[float]]]:
    from apps.rag.models import KnowledgeChunk

    rows = (
        KnowledgeChunk.objects
        .filter(user=user, website=website)
        .only("id", "embedding")[:MAX_CHUNKS]
    )
    return [(str(c.id), c.embedding) for c in rows if c.embedding]


def _best_match(vec, fact_vecs, chunk_vecs):
    """Best cosine over all dimension-compatible comparators.

    Returns ``(similarity, kind, ref_id)``. Length mismatches are skipped
    (retriever precedent) — this also neutralizes the 32/256/1536
    cross-fallback incomparability quirk.
    """
    best = (0.0, "", "")
    for fact, fvec in fact_vecs:
        if len(fvec) != len(vec):
            continue
        score = cosine_similarity(vec, fvec)
        if score > best[0]:
            best = (score, "fact", str(fact.id))
    for chunk_id, cvec in chunk_vecs:
        if len(cvec) != len(vec):
            continue
        score = cosine_similarity(vec, cvec)
        if score > best[0]:
            best = (score, "chunk", chunk_id)
    return best


def compute_alignment(result) -> AlignmentResult:
    """Benchmark one stored response against the brand knowledge base."""
    audit = result.audit
    website = audit.website
    # Two distinct users here. Brand Input chunks are scoped to the
    # website owner, which is who ingested them — audit.created_by can
    # differ (e.g. seeded audits). SPEND, however, belongs to whoever
    # caused the work: the audit's creator, falling back to the owner
    # for system-generated audits.
    kb_user = getattr(website, "user", None) or audit.created_by
    spend_user = audit.created_by or getattr(website, "user", None)
    actor = "user" if audit.created_by else "system"

    facts = _load_facts(website)
    chunks = _load_chunks(kb_user, website)
    if not facts and not chunks:
        return _skip(STATUS_NO_BRAND_INPUT)

    terms = brand_terms(website, {})
    brand_name = (audit.business_name or "").strip()
    if brand_name and not any(t.lower() == brand_name.lower() for t in terms):
        terms = [brand_name] + terms

    units = _extract_units(result.response_text or "", terms)
    brand_units = [u for u, is_brand in units if is_brand]
    if not brand_units:
        return _skip(STATUS_NO_BRAND_CLAIMS)

    all_unit_texts = [u for u, _ in units]
    prompt_text = (result.prompt or "")[:2000]
    vectors, model, _dim = embed_texts(
        [prompt_text] + all_unit_texts,
        user=spend_user,
        website=website,
        metadata={
            "role": "alignment",
            "actor": actor,
            "audit_id": str(result.audit_id),
            "result_id": str(result.id),
        },
    )
    if model == FALLBACK_MODEL:
        # Hash vectors are deliberately non-semantic; a similarity score
        # computed from them is noise dressed as a number.
        return _skip(STATUS_EMBEDDINGS_UNAVAILABLE)
    if len(vectors) != len(all_unit_texts) + 1:
        logger.warning("alignment embed count mismatch for result %s", result.id)
        return _skip(STATUS_EMBEDDINGS_UNAVAILABLE)

    prompt_vec = vectors[0]
    unit_vecs = dict(zip(all_unit_texts, vectors[1:], strict=False))

    # ── Direction (a): claim support ──
    scored_units = brand_units[:MAX_BRAND_UNITS]
    supported_samples: list[dict] = []
    unsupported_samples: list[dict] = []
    supported = 0
    for unit in scored_units:
        vec = unit_vecs.get(unit) or []
        best, kind, ref_id = _best_match(vec, facts, chunks) if vec else (0.0, "", "")
        if best >= SUPPORT_THRESHOLD:
            supported += 1
            if len(supported_samples) < MAX_SAMPLES:
                supported_samples.append({
                    "text": unit[:MAX_SAMPLE_TEXT],
                    "match_kind": kind,
                    "match_id": ref_id,
                    "similarity": round(best, 2),
                })
        elif len(unsupported_samples) < MAX_SAMPLES:
            unsupported_samples.append({
                "text": unit[:MAX_SAMPLE_TEXT],
                "best_similarity": round(best, 2),
            })
    support = supported / len(scored_units)

    # ── Direction (b): message coverage (facts only) ──
    coverage = None
    coverage_detail = None
    if facts:
        relevant = []
        for fact, fvec in facts:
            if len(fvec) != len(prompt_vec):
                continue
            relevance = cosine_similarity(prompt_vec, fvec)
            if relevance >= RELEVANCE_FLOOR:
                relevant.append((relevance, fact, fvec))
        relevant.sort(key=lambda t: t[0], reverse=True)
        top = relevant[:COVERAGE_K]
        if top:
            reflected: list[dict] = []
            missing: list[dict] = []
            for relevance, fact, fvec in top:
                best = 0.0
                # A brand message can be reflected by a sentence that does
                # not name the brand ("they offer a 30-day guarantee"), so
                # coverage checks every unit, not just brand-scoped ones.
                for vec in unit_vecs.values():
                    if len(vec) != len(fvec):
                        continue
                    score = cosine_similarity(vec, fvec)
                    if score > best:
                        best = score
                if best >= COVERAGE_THRESHOLD:
                    if len(reflected) < MAX_COVERAGE_ITEMS:
                        reflected.append({
                            "fact_id": str(fact.id),
                            "text": _fact_text(fact),
                            "similarity": round(best, 2),
                        })
                else:
                    if len(missing) < MAX_COVERAGE_ITEMS:
                        missing.append({
                            "fact_id": str(fact.id),
                            "text": _fact_text(fact),
                            "prompt_relevance": round(relevance, 2),
                        })
            coverage = len(reflected) / len(top)
            coverage_detail = {
                "score": round(coverage, 2),
                "k": len(top),
                "reflected": reflected,
                "missing": missing,
            }

    # ── Composite ──
    if coverage is not None:
        score = 100 * (SUPPORT_WEIGHT * support + COVERAGE_WEIGHT * coverage)
        basis = "facts+chunks" if chunks else "facts"
    else:
        score = 100 * support
        basis = "chunks_only"

    detail = {
        "version": ALIGNMENT_VERSION,
        "status": STATUS_SCORED,
        "basis": basis,
        "support": {
            "score": round(support, 2),
            "total_units": len(scored_units),
            "supported": supported,
            "supported_samples": supported_samples,
            "unsupported_samples": unsupported_samples,
        },
        "counts": {"facts": len(facts), "chunks": len(chunks)},
    }
    if coverage_detail is not None:
        detail["coverage"] = coverage_detail

    return AlignmentResult(
        score=round(score, 1),
        status=STATUS_SCORED,
        detail=detail,
        model=model,
    )
