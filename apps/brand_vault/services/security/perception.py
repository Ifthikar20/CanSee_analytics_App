"""Security perception: what AI assistants say about a brand's security.

Two jobs:

* :func:`score_claim_support` — for one security-probe answer, match each
  extracted claim against the brand's own security and privacy facts (and
  knowledge chunks) by embedding similarity and record whether the brand's
  material supports it. Embedding-only, no LLM call, so it can run on
  every claim of every probe.
* :func:`build_security_perception` — the dashboard numbers for a website
  over a window: mention rate with the same Beta-Binomial smoothing and
  Wilson interval the visibility score uses, claim accuracy, hallucinated
  compliance and unconfirmed incident counts, fear and advise-against
  rates, private-data leakage, competitor trust share, cited source
  classes, open security findings, a per-model breakdown and a per-probe
  trend.

Everything here is PERCEPTION. A model saying a brand is SOC 2 certified
does not make it so, and an unsupported claim means the brand's uploaded
material does not back it, not that the claim is false. The API labels
carry that distinction and the UI copy must keep it.
"""
from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import timedelta

from django.utils import timezone

from apps.brand_vault.services.brand_alignment import (
    SUPPORT_THRESHOLD,
    _best_match,
    _load_chunks,
    _load_facts,
)
from apps.brand_vault.services.fact_extractor import SECURITY_FACT_TOPICS
from apps.llm_ranking.services.security_lexicon import (
    KIND_CERTIFICATION,
    KIND_INCIDENT,
    POLARITY_AFFIRMS,
    SUPPORT_SUPPORTED,
    SUPPORT_UNKNOWN,
    SUPPORT_UNSUPPORTED,
)
from apps.rag.services.embedder import FALLBACK_MODEL, embed_texts

logger = logging.getLogger("apps")

SUPPORT_VERSION = "v1"

STATUS_SCORED = "scored"
STATUS_NO_BRAND_INPUT = "no_brand_input"
STATUS_EMBEDDINGS_UNAVAILABLE = "embeddings_unavailable"
STATUS_NO_CLAIMS = "no_claims"

# Security findings the perception page reports on. BS-PRIV-001 is the
# existing private-data detector; the rest ship with this feature.
SECURITY_DETECTOR_CODES: tuple[str, ...] = (
    "BS-SEC-001", "BS-SEC-002", "BS-SEC-003", "BS-PRIV-001",
)
PRIVATE_DATA_DETECTOR = "BS-PRIV-001"

DEFAULT_WINDOW_DAYS = 90
MAX_TREND_POINTS = 12
MAX_CLAIMS_PER_RESULT = 20


# ── Claim support ────────────────────────────────────────────────────────

def _mark_all(claims: list[dict], support: str) -> None:
    for c in claims:
        c["support"] = support
        c.setdefault("similarity", None)


def score_claim_support(result) -> dict:
    """Annotate ``result.security_claims`` in place and persist it.

    Each claim gains ``support`` (supported / unsupported / unknown),
    ``similarity`` and, when supported, ``match_kind`` / ``match_id``.
    The envelope gains ``support_version`` and ``support_status``.
    Returns the updated dict.
    """
    payload = dict(result.security_claims or {})
    claims = [c for c in (payload.get("claims") or []) if isinstance(c, dict)]
    claims = claims[:MAX_CLAIMS_PER_RESULT]
    payload["support_version"] = SUPPORT_VERSION

    if not claims:
        payload["support_status"] = STATUS_NO_CLAIMS
        return _persist(result, payload)

    audit = result.audit
    website = audit.website
    kb_user = getattr(website, "user", None) or audit.created_by
    spend_user = audit.created_by or getattr(website, "user", None)

    # Security-tagged facts first; older fact bases predate topic tags, so
    # fall back to every approved fact rather than pretend there is none.
    facts = _load_facts(website, topics=SECURITY_FACT_TOPICS) or _load_facts(website)
    chunks = _load_chunks(kb_user, website)
    if not facts and not chunks:
        _mark_all(claims, SUPPORT_UNKNOWN)
        payload["claims"] = claims
        payload["support_status"] = STATUS_NO_BRAND_INPUT
        return _persist(result, payload)

    texts = [c.get("claim", "")[:500] for c in claims]
    vectors, model, _dim = embed_texts(
        texts,
        user=spend_user,
        website=website,
        metadata={
            "role": "security_claim_support",
            "actor": "user" if audit.created_by else "system",
            "audit_id": str(result.audit_id),
            "result_id": str(result.id),
        },
    )
    if model == FALLBACK_MODEL or len(vectors) != len(claims):
        _mark_all(claims, SUPPORT_UNKNOWN)
        payload["claims"] = claims
        payload["support_status"] = STATUS_EMBEDDINGS_UNAVAILABLE
        return _persist(result, payload)

    for claim, vec in zip(claims, vectors, strict=False):
        best, kind, ref_id = _best_match(vec, facts, chunks) if vec else (0.0, "", "")
        claim["similarity"] = round(best, 2)
        if best >= SUPPORT_THRESHOLD:
            claim["support"] = SUPPORT_SUPPORTED
            claim["match_kind"] = kind
            claim["match_id"] = ref_id
        else:
            claim["support"] = SUPPORT_UNSUPPORTED
            claim.pop("match_kind", None)
            claim.pop("match_id", None)
    payload["claims"] = claims
    payload["support_status"] = STATUS_SCORED
    payload["support_model"] = model
    return _persist(result, payload)


def _persist(result, payload: dict) -> dict:
    result.security_claims = payload
    result.save(update_fields=["security_claims", "updated_at"])
    return payload


# ── Aggregation ──────────────────────────────────────────────────────────

def _pct(part: int, whole: int) -> float | None:
    if whole <= 0:
        return None
    return round(100.0 * part / whole, 1)


class _ClaimTally:
    """Running counts over a set of results' security_claims."""

    def __init__(self):
        self.responses = 0
        self.mentioned = 0
        self.fud = 0
        self.against = 0
        self.heuristic = 0
        self.claims = 0
        self.affirmed = 0
        self.scored_affirmed = 0
        self.supported_affirmed = 0
        self.hallucinated_compliance = 0
        self.incident_claims = 0
        self.unconfirmed_incidents = 0
        self.by_kind: dict[str, Counter] = defaultdict(Counter)

    def add(self, result) -> None:
        self.responses += 1
        if result.is_mentioned:
            self.mentioned += 1
        payload = result.security_claims if isinstance(result.security_claims, dict) else {}
        if payload.get("fud_language"):
            self.fud += 1
        if payload.get("recommends_against"):
            self.against += 1
        if payload.get("model") == "heuristic":
            self.heuristic += 1
        for c in payload.get("claims") or []:
            if not isinstance(c, dict):
                continue
            kind = c.get("kind") or "other"
            support = c.get("support") or SUPPORT_UNKNOWN
            affirms = c.get("polarity") == POLARITY_AFFIRMS
            self.claims += 1
            self.by_kind[kind]["claims"] += 1
            if not affirms:
                continue
            self.affirmed += 1
            self.by_kind[kind]["affirmed"] += 1
            if support in (SUPPORT_SUPPORTED, SUPPORT_UNSUPPORTED):
                self.scored_affirmed += 1
                self.by_kind[kind]["scored"] += 1
                if support == SUPPORT_SUPPORTED:
                    self.supported_affirmed += 1
                    self.by_kind[kind]["supported"] += 1
                else:
                    self.by_kind[kind]["unsupported"] += 1
            if kind == KIND_INCIDENT:
                self.incident_claims += 1
                if support == SUPPORT_UNSUPPORTED:
                    self.unconfirmed_incidents += 1
            if kind == KIND_CERTIFICATION and support == SUPPORT_UNSUPPORTED:
                self.hallucinated_compliance += 1

    @property
    def claim_accuracy(self) -> float | None:
        return _pct(self.supported_affirmed, self.scored_affirmed)

    def rates(self) -> dict:
        n = self.responses
        return {
            "responses": n,
            "mention_rate": _pct(self.mentioned, n),
            "claim_accuracy": self.claim_accuracy,
            "fud_rate": _pct(self.fud, n),
            "recommends_against_rate": _pct(self.against, n),
            "heuristic_share": _pct(self.heuristic, n),
        }


def _window_start(days: int):
    if days <= 0:
        return None
    return timezone.now() - timedelta(days=days)


def build_security_perception(website, *, days: int = DEFAULT_WINDOW_DAYS) -> dict:
    """Aggregate security-perception metrics for ``website``."""
    from apps.brand_vault.models import SafetyAlert
    from apps.llm_ranking.models import LLMRankingAudit, LLMRankingResult
    from apps.llm_ranking.services.ranking_service import beta_binomial_mean, wilson_ci

    audits_qs = LLMRankingAudit.objects.filter(
        website=website,
        probe_kind=LLMRankingAudit.PROBE_KIND_SECURITY,
    )
    active = (
        audits_qs.filter(
            status__in=(LLMRankingAudit.STATUS_PENDING, LLMRankingAudit.STATUS_RUNNING),
        )
        .order_by("-created_at")
        .values("id", "status", "queries_completed", "total_queries")
        .first()
    )
    completed = audits_qs.filter(status=LLMRankingAudit.STATUS_COMPLETED)
    start = _window_start(days)
    if start is not None:
        completed = completed.filter(completed_at__gte=start)
    audits = list(completed.order_by("completed_at"))

    out: dict = {
        "window_days": days,
        "probes": len(audits),
        "active_probe": (
            {
                "id": str(active["id"]),
                "status": active["status"],
                "queries_completed": active["queries_completed"],
                "total_queries": active["total_queries"],
            }
            if active else None
        ),
        "last_probe_at": audits[-1].completed_at if audits else None,
        "latest_audit_id": str(audits[-1].id) if audits else None,
        "brand_name": (
            (audits[-1].business_name if audits else "") or getattr(website, "name", "") or ""
        ),
    }
    if not audits:
        out.update(_empty_metrics())
        return out

    audit_ids = [a.id for a in audits]
    results = list(
        LLMRankingResult.objects
        .filter(audit_id__in=audit_ids, query_succeeded=True)
        .only(
            "id", "audit_id", "provider", "model_id", "is_mentioned",
            "security_claims", "created_at",
        )
    )

    overall = _ClaimTally()
    per_model: dict[str, _ClaimTally] = defaultdict(_ClaimTally)
    per_audit: dict = defaultdict(_ClaimTally)
    for r in results:
        overall.add(r)
        per_model[r.provider].add(r)
        per_audit[r.audit_id].add(r)

    n = overall.responses
    lo, hi = wilson_ci(overall.mentioned, n) if n else (0.0, 0.0)
    out["mention_rate"] = {
        "raw": _pct(overall.mentioned, n),
        "smoothed": round(100.0 * beta_binomial_mean(overall.mentioned, n), 1) if n else None,
        "ci_lower": round(100.0 * lo, 1),
        "ci_upper": round(100.0 * hi, 1),
        "mentioned": overall.mentioned,
        "n": n,
    }
    out["claims"] = {
        "total": overall.claims,
        "affirmed": overall.affirmed,
        "scored": overall.scored_affirmed,
        "supported": overall.supported_affirmed,
        "unsupported": overall.scored_affirmed - overall.supported_affirmed,
        "accuracy": overall.claim_accuracy,
        "by_kind": {k: dict(v) for k, v in sorted(overall.by_kind.items())},
    }
    out["hallucinated_compliance"] = overall.hallucinated_compliance
    out["incident_claims"] = overall.incident_claims
    out["unconfirmed_incidents"] = overall.unconfirmed_incidents
    out["fud_rate"] = _pct(overall.fud, n)
    out["recommends_against_rate"] = _pct(overall.against, n)
    out["heuristic_share"] = _pct(overall.heuristic, n)

    result_ids = [r.id for r in results]
    alerts = SafetyAlert.objects.filter(website=website, result_id__in=result_ids)
    leaked = (
        alerts.filter(detector_code=PRIVATE_DATA_DETECTOR)
        .values("result_id").distinct().count()
    )
    out["leakage_rate"] = _pct(leaked, n)
    open_counts = Counter(
        alerts.filter(
            status=SafetyAlert.STATUS_OPEN,
            detector_code__in=SECURITY_DETECTOR_CODES,
        ).values_list("detector_code", flat=True)
    )
    out["open_findings"] = {code: open_counts.get(code, 0) for code in SECURITY_DETECTOR_CODES}

    latest = audits[-1]
    strengths = latest.brand_strengths if isinstance(latest.brand_strengths, dict) else {}
    brand_name = out["brand_name"]
    brand_strength = None
    for name, value in strengths.items():
        if name.strip().lower() == brand_name.strip().lower():
            brand_strength = value
            break
    out["trust_share"] = {
        "brand": brand_strength,
        "brands": strengths,
        "audit_id": str(latest.id),
    }

    out["citation_sources"] = _citation_sources(result_ids)

    out["by_model"] = [
        {"provider": provider, **tally.rates()}
        for provider, tally in sorted(per_model.items())
    ]

    trend = []
    for audit in audits[-MAX_TREND_POINTS:]:
        tally = per_audit.get(audit.id) or _ClaimTally()
        trend.append({
            "audit_id": str(audit.id),
            "completed_at": audit.completed_at,
            **tally.rates(),
        })
    out["trend"] = trend
    return out


def _empty_metrics() -> dict:
    return {
        "mention_rate": {
            "raw": None, "smoothed": None, "ci_lower": 0.0, "ci_upper": 0.0,
            "mentioned": 0, "n": 0,
        },
        "claims": {
            "total": 0, "affirmed": 0, "scored": 0, "supported": 0,
            "unsupported": 0, "accuracy": None, "by_kind": {},
        },
        "hallucinated_compliance": 0,
        "incident_claims": 0,
        "unconfirmed_incidents": 0,
        "fud_rate": None,
        "recommends_against_rate": None,
        "heuristic_share": None,
        "leakage_rate": None,
        "open_findings": {code: 0 for code in SECURITY_DETECTOR_CODES},
        "trust_share": {"brand": None, "brands": {}, "audit_id": None},
        "citation_sources": {},
        "by_model": [],
        "trend": [],
    }


def _citation_sources(result_ids: list) -> dict:
    """Source-class distribution of the pages cited in these answers."""
    try:
        from apps.citations.models import Citation
        rows = (
            Citation.objects
            .filter(result_id__in=result_ids)
            .values_list("source_class", flat=True)
        )
        return dict(Counter(rows))
    except Exception as exc:  # pragma: no cover - citations app optional here
        logger.debug("citation source breakdown unavailable: %s", exc)
        return {}
