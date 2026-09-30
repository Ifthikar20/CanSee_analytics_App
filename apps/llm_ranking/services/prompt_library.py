"""
Prompt library for LLM Ranking audits.

The catalogue lives in JSON files under `apps/llm_ranking/prompt_packs/`:

  default.json        — industry-agnostic base set (always loaded)
  saas.json           — B2B SaaS
  ecommerce.json      — e-commerce / DTC
  legal.json          — law firms and legal services
  agency.json         — marketing / creative agencies
  healthcare.json     — clinics and telehealth

Each pack ships with a `industry_keys` list; when an audit's industry string
matches any of those keys (case-insensitive substring match), the pack is
layered over the default set. The final mix is intent-balanced so no single
query shape dominates the benchmark.

Why JSON instead of Python:
- Non-engineers can edit prompts without a deploy (PR on a json file).
- Industry packs are self-contained and trivial to add (drop a new file).
- Future `PromptPack` Django model can persist overrides above these defaults
  without redesigning the schema.

Each JSON pack matches this shape:

  {
    "schema": "prompt-pack/v1",
    "name": "...",
    "description": "...",
    "industry_keys": ["..."],
    "prompts": [
      {"text": "...", "intent": "recommendation", "placeholders": ["industry"]}
    ]
  }
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger("apps")

PACK_DIR = Path(__file__).resolve().parent.parent / "prompt_packs"
_PLACEHOLDER_RE = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}")
DEFAULT_PACK = "default"

# Intent weighting. Controls how many prompts from each intent bucket the
# sampler tries to take. Tunable here (not in JSON) because it's about
# sampling strategy, not prompt content.
INTENT_PRIORITY: tuple[tuple[str, int], ...] = (
    ("recommendation", 2),
    ("comparison",     2),
    ("use_case",       2),
    ("alternatives",   1),
    ("category",       1),
    ("persona",        1),
    ("review",         1),
    ("local",          1),
)

VISIBILITY_INTENTS: frozenset = frozenset(i for i, _ in INTENT_PRIORITY)

# Security perception probes. These prompts NAME the brand on purpose: the
# question is what the model says about the brand's security, compliance
# and data handling, not whether the brand surfaces unprompted. They live
# in their own pack (security.json) and are only sampled for audits with
# probe_kind == "security", so a visibility audit never mixes them in.
SECURITY_PACK = "security"
SECURITY_INTENT_PRIORITY: tuple[tuple[str, int], ...] = (
    ("compliance",       2),
    ("incident_history", 2),
    ("data_handling",    2),
    ("trust_comparison", 2),
    ("vulnerability",    1),
    ("privacy",          1),
)
SECURITY_INTENTS: frozenset = frozenset(i for i, _ in SECURITY_INTENT_PRIORITY)

PROBE_KIND_VISIBILITY = "visibility"
PROBE_KIND_SECURITY = "security"

VALID_INTENTS: frozenset = VISIBILITY_INTENTS | SECURITY_INTENTS


# Funnel stages for grouping prompts in the UI. Same idea as marketing
# funnel: bottom-of-funnel = direct intent (the user already knows what
# they want), top-of-funnel = problem unaware. Mapping an intent to a
# funnel stage lets the Prompts table group strategically rather than
# by query shape.
FUNNEL_BOTTOM = "bottom"   # user is ready to pick — high-stakes prompts
FUNNEL_MID = "mid"          # solution-aware — comparison and price intent
FUNNEL_TOP = "top"          # awareness / editorial / thought leadership
FUNNEL_NICHE = "niche"      # long-tail, persona-specific, custom
FUNNEL_TRUST = "trust"      # security / compliance / privacy due diligence

INTENT_FUNNEL_STAGE: dict[str, str] = {
    "recommendation": FUNNEL_BOTTOM,
    "alternatives":   FUNNEL_BOTTOM,
    "review":         FUNNEL_BOTTOM,
    "comparison":     FUNNEL_MID,
    "use_case":       FUNNEL_MID,
    "category":       FUNNEL_MID,
    "persona":        FUNNEL_TOP,
    "local":          FUNNEL_NICHE,
    "custom":         FUNNEL_NICHE,
    "compliance":       FUNNEL_TRUST,
    "incident_history": FUNNEL_TRUST,
    "data_handling":    FUNNEL_TRUST,
    "trust_comparison": FUNNEL_TRUST,
    "vulnerability":    FUNNEL_TRUST,
    "privacy":          FUNNEL_TRUST,
}

FUNNEL_STAGE_LABELS: dict[str, str] = {
    FUNNEL_BOTTOM: "Bottom of Funnel — High Intent",
    FUNNEL_MID:    "Mid Funnel — Category & Comparison",
    FUNNEL_TOP:    "Top of Funnel — Awareness",
    FUNNEL_NICHE:  "Niche / Long-Tail",
    FUNNEL_TRUST:  "Trust and Security Due Diligence",
}

# Per-intent rationale templates. {business_name}, {industry} and
# {use_case} are interpolated at generate time. Plain text — these
# render under each prompt row in the UI to coach the user on what
# the prompt actually tests.
INTENT_RATIONALE: dict[str, str] = {
    "recommendation": (
        "Direct discovery query. If {business_name} doesn't surface here, "
        "nothing else in the prompt run matters — this is the prompt your buyers "
        "actually run."
    ),
    "alternatives": (
        "Competitor-comparison prompt. Tests whether AI places {business_name} "
        "in the same set as the incumbents in {industry}."
    ),
    "review": (
        "Brand-name trust query — the first thing someone searches after "
        "hearing about {business_name}. A weak answer here costs you the "
        "decision; aim for an accurate, positive description."
    ),
    "comparison": (
        "Side-by-side comparison intent. Tests whether AI knows you exist "
        "alongside the {industry} alternatives buyers shortlist."
    ),
    "use_case": (
        "Problem-aware but not solution-aware. Mirrors the use case "
        "{business_name} solves; surfacing here means the AI understands "
        "your category, not just your name."
    ),
    "category": (
        "Category-bucket query. Tests whether AI associates {business_name} "
        "with the broader {industry} space."
    ),
    "persona": (
        "Editorial / listicle prompt — the kind a journalist or analyst "
        "would write. Tests your brand awareness and PR footprint."
    ),
    "local": (
        "Geographic intent. Tests local SEO signals and place-based "
        "mentions for {business_name}."
    ),
    "custom": (
        "Custom prompt — tests a specific phrasing or persona you supplied."
    ),
    "compliance": (
        "Compliance question. Tests whether AI states {business_name}'s "
        "certifications and regulatory status accurately, or invents them."
    ),
    "incident_history": (
        "Incident-history question. Tests whether AI attributes breaches, "
        "hacks or vulnerabilities to {business_name} that never happened, "
        "or keeps repeating ones that were resolved."
    ),
    "data_handling": (
        "Data-handling question. Tests what AI says about how "
        "{business_name} stores, encrypts and controls access to customer data."
    ),
    "trust_comparison": (
        "Trust comparison. Tests whether AI would steer a security-conscious "
        "buyer toward or away from {business_name} against {industry} peers."
    ),
    "vulnerability": (
        "Weakness probe. Tests what AI volunteers as {business_name}'s "
        "security weaknesses and whether that is grounded in fact."
    ),
    "privacy": (
        "Privacy question. Tests what AI says about the personal data "
        "{business_name} collects, retains and shares."
    ),
}


def rationale_for(intent: str, *, business_name: str = "your brand",
                  industry: str = "this category", use_case: str = "") -> str:
    """Render the rationale for a prompt of the given intent."""
    template = INTENT_RATIONALE.get(intent, INTENT_RATIONALE["custom"])
    try:
        return template.format(
            business_name=business_name or "your brand",
            industry=industry or "this category",
            use_case=use_case or industry or "this category",
        )
    except (KeyError, IndexError):
        return template


def funnel_stage_for(intent: str) -> str:
    return INTENT_FUNNEL_STAGE.get(intent, FUNNEL_NICHE)


@dataclass(frozen=True)
class PromptTemplate:
    text: str
    intent: str
    placeholders: frozenset = field(default_factory=frozenset)

    def fill(self, **kwargs) -> str:
        safe = {k: v for k, v in kwargs.items() if k in self.placeholders}
        return self.text.format(**safe) if self.placeholders else self.text


@dataclass(frozen=True)
class PromptPack:
    name: str
    description: str
    industry_keys: tuple[str, ...]
    templates: tuple[PromptTemplate, ...]


# ── Pack loading ──────────────────────────────────────────────────────────────

def _parse_pack(data: dict) -> PromptPack:
    """Convert a raw JSON dict into a PromptPack. Drops malformed entries."""
    templates: list[PromptTemplate] = []
    for raw in data.get("prompts", []) or []:
        if not isinstance(raw, dict):
            continue
        text = (raw.get("text") or "").strip()
        intent = (raw.get("intent") or "").strip()
        if not text or intent not in VALID_INTENTS:
            continue
        placeholders = frozenset(raw.get("placeholders") or [])
        templates.append(PromptTemplate(text=text, intent=intent, placeholders=placeholders))
    return PromptPack(
        name=str(data.get("name") or "Unnamed"),
        description=str(data.get("description") or ""),
        industry_keys=tuple(str(k).lower() for k in (data.get("industry_keys") or [])),
        templates=tuple(templates),
    )


@lru_cache(maxsize=32)
def load_pack(name: str) -> PromptPack | None:
    """Load a single pack by filename stem. Returns None if the file is missing."""
    path = PACK_DIR / f"{name}.json"
    if not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        logger.warning("Failed to load prompt pack %s: %s", name, exc)
        return None
    return _parse_pack(data)


def list_available_packs() -> list[str]:
    """Names of all JSON files in the pack directory (without extension)."""
    if not PACK_DIR.is_dir():
        return []
    return sorted(p.stem for p in PACK_DIR.glob("*.json"))


def _industry_packs() -> list[PromptPack]:
    """Every pack except the default, loaded and cached."""
    packs: list[PromptPack] = []
    for name in list_available_packs():
        # The security pack is probe-kind scoped, never industry-matched.
        if name in (DEFAULT_PACK, SECURITY_PACK):
            continue
        pack = load_pack(name)
        if pack is not None:
            packs.append(pack)
    return packs


def security_templates() -> tuple[PromptTemplate, ...]:
    """Templates of the security pack (empty tuple if the file is missing)."""
    pack = load_pack(SECURITY_PACK)
    return pack.templates if pack else ()


def match_industry_pack(industry: str) -> PromptPack | None:
    """Return the first industry pack whose keys match the given industry string.

    Matching is case-insensitive substring: pack key "saas" matches
    "SaaS analytics" or "B2B SaaS". First match wins (packs are scanned in
    alphabetical order).
    """
    if not industry:
        return None
    haystack = industry.lower()
    for pack in _industry_packs():
        for key in pack.industry_keys:
            if key and key in haystack:
                return pack
    return None


# ── Sampling ─────────────────────────────────────────────────────────────────

def _dedupe_preserving_order(items: Iterable[str]) -> list[str]:
    seen: set = set()
    out: list[str] = []
    for item in items:
        key = item.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(item.strip())
    return out


def _bucket_by_intent(templates: Iterable[PromptTemplate]) -> dict[str, list[PromptTemplate]]:
    buckets: dict[str, list[PromptTemplate]] = {intent: [] for intent in VALID_INTENTS}
    for t in templates:
        buckets.setdefault(t.intent, []).append(t)
    return buckets


class PromptLibrary:
    """
    Generates an intent-balanced prompt set for an audit.

    The default pack is always loaded. If `industry` matches one of the
    industry packs, its templates are layered in FIRST within each intent
    bucket so the industry-specific phrasings take priority over the
    generic defaults.
    """

    DEFAULT_MAX = 10

    # ── Public API ────────────────────────────────────────────────────────

    @classmethod
    def generate(
        cls,
        *,
        industry: str,
        use_case: str = "",
        location: str = "",
        max_prompts: int = DEFAULT_MAX,
        themes: list | None = None,
        business_name: str = "",
        probe_kind: str = PROBE_KIND_VISIBILITY,
        competitor: str = "",
    ) -> list[dict]:
        """Return a list of dicts:
            {"text": str, "intent": str, "funnel_stage": str, "rationale": str}

        `themes` (optional) restricts sampling to a subset of intents.
        Empty / None means use the default INTENT_PRIORITY mix.

        ``probe_kind="security"`` switches to the security pack: prompts
        that name the brand and ask about compliance, incidents, data
        handling, trust, weaknesses and privacy. ``competitor`` fills the
        ``{competitor}`` slot; templates that need it are skipped when it
        is empty, the same way ``local`` is skipped without a location.
        """
        if probe_kind == PROBE_KIND_SECURITY:
            return cls._generate_security(
                industry=industry,
                business_name=business_name,
                competitor=competitor,
                max_prompts=max_prompts,
                themes=themes,
            )
        # No hardcoded "software" default: a blank industry used to turn a
        # car / finance / agency site into a generic SaaS prompt list. A
        # neutral noun keeps templates grammatical without mislabelling the
        # business (the LLM sample generator is the real fix for a blank
        # industry — this only guards direct template callers).
        industry_norm = (industry or "this category").strip()
        # Default use_case is a noun phrase built AROUND the industry, not
        # the bare industry string — otherwise templates like "best
        # {industry} platform for {use_case}?" render "best cyber platform
        # for cyber?" (and every real industry input is a noun, so the
        # phrase slots keep working). Callers with a real use case
        # override this untouched.
        use_case_norm = (use_case or f"a growing {industry_norm} business").strip()
        location_norm = (location or "").strip()
        allowed = {t for t in (themes or []) if t in VALID_INTENTS} or None

        templates = cls._resolved_templates(industry_norm)
        buckets = _bucket_by_intent(templates)

        picked: list[dict] = []
        seen_texts: set = set()

        for intent, take_n in INTENT_PRIORITY:
            if allowed is not None and intent not in allowed:
                continue
            # Skip `local` unless a location is provided — filling without
            # location yields broken prompts.
            if intent == "local" and not location_norm:
                continue
            bucket = buckets.get(intent, [])
            taken = 0
            for template in bucket:
                if taken >= take_n:
                    break
                try:
                    text = template.fill(
                        industry=industry_norm,
                        use_case=use_case_norm,
                        location=location_norm,
                    )
                except KeyError:
                    continue
                key = text.strip().lower()
                if not text or key in seen_texts:
                    continue
                seen_texts.add(key)
                picked.append({
                    "text": text,
                    "intent": intent,
                    "funnel_stage": funnel_stage_for(intent),
                    "rationale": rationale_for(
                        intent,
                        business_name=business_name,
                        industry=industry_norm,
                        use_case=use_case_norm,
                    ),
                })
                taken += 1
                if len(picked) >= max_prompts:
                    return picked

        return picked

    @classmethod
    def _generate_security(
        cls,
        *,
        industry: str,
        business_name: str,
        competitor: str = "",
        max_prompts: int = DEFAULT_MAX,
        themes: list | None = None,
    ) -> list[dict]:
        """Intent-balanced sample from the security pack.

        Every template names the brand, so an empty ``business_name``
        yields nothing: there is no honest way to ask "has X had a breach"
        without X. Callers turn an empty list into a refusal.
        """
        brand = (business_name or "").strip()
        if not brand:
            logger.warning("security probe requested without a business name")
            return []
        industry_norm = (industry or "this category").strip()
        competitor_norm = (competitor or "").strip()
        allowed = {t for t in (themes or []) if t in SECURITY_INTENTS} or None

        buckets = _bucket_by_intent(security_templates())
        picked: list[dict] = []
        seen_texts: set = set()
        for intent, take_n in SECURITY_INTENT_PRIORITY:
            if allowed is not None and intent not in allowed:
                continue
            taken = 0
            for template in buckets.get(intent, []):
                if taken >= take_n:
                    break
                if "competitor" in template.placeholders and not competitor_norm:
                    continue
                try:
                    text = template.fill(
                        brand=brand,
                        industry=industry_norm,
                        competitor=competitor_norm,
                    )
                except KeyError:
                    continue
                key = text.strip().lower()
                if not text or key in seen_texts:
                    continue
                seen_texts.add(key)
                picked.append({
                    "text": text,
                    "intent": intent,
                    "funnel_stage": funnel_stage_for(intent),
                    "rationale": rationale_for(
                        intent, business_name=brand, industry=industry_norm,
                    ),
                })
                taken += 1
                if len(picked) >= max_prompts:
                    return picked
        return picked

    @classmethod
    def generate_texts(
        cls,
        *,
        industry: str,
        use_case: str = "",
        location: str = "",
        max_prompts: int = DEFAULT_MAX,
        themes: list | None = None,
    ) -> list[str]:
        return [
            p["text"] for p in cls.generate(
                industry=industry,
                use_case=use_case,
                location=location,
                max_prompts=max_prompts,
                themes=themes,
            )
        ]

    @classmethod
    def intents_for(cls, prompt_texts: Iterable[str]) -> list[str]:
        """Best-effort intent tag for arbitrary prompt text.

        Uses substring matching against every known template (with
        placeholders stripped) so a generated-and-filled prompt still maps
        back to its source intent.
        """
        # Each template becomes its ordered literal fragments (the text
        # between placeholders). A prompt matches when every fragment of
        # at least four characters appears in order, so "Is {brand} SOC 2
        # compliant?" still maps once the brand is filled in mid-sentence.
        index: list[tuple[tuple[str, ...], str]] = []
        for t in (*cls._resolved_templates(industry=""), *security_templates()):
            fragments = tuple(
                " ".join(frag.split())
                for frag in _PLACEHOLDER_RE.split(t.text.lower())
                if len(" ".join(frag.split())) >= 4
            )
            if fragments:
                index.append((fragments, t.intent))

        out: list[str] = []
        for text in prompt_texts:
            lower = " ".join(text.lower().split())
            matched = "custom"
            for fragments, intent in index:
                pos = 0
                for frag in fragments:
                    found = lower.find(frag, pos)
                    if found < 0:
                        break
                    pos = found + len(frag)
                else:
                    matched = intent
                    break
            out.append(matched)
        return out

    @classmethod
    def resolved_pack_names(cls, industry: str) -> list[str]:
        """Debug / UI helper: which packs are active for this industry?"""
        used = [DEFAULT_PACK]
        pack = match_industry_pack(industry)
        if pack is not None:
            # Find the filename for this pack so callers can show "saas"
            for name in list_available_packs():
                if name == DEFAULT_PACK:
                    continue
                loaded = load_pack(name)
                if loaded is not None and loaded.name == pack.name:
                    used.append(name)
                    break
        return used

    # ── Internals ─────────────────────────────────────────────────────────

    @classmethod
    def _resolved_templates(cls, industry: str) -> tuple[PromptTemplate, ...]:
        """Combine the default pack with the matching industry pack (if any).

        Industry templates come first so the sampler prefers them when
        filling each intent bucket, before falling back to default phrasings.
        """
        default = load_pack(DEFAULT_PACK)
        default_templates = default.templates if default else ()
        industry_pack = match_industry_pack(industry) if industry else None
        industry_templates = industry_pack.templates if industry_pack else ()
        # Dedupe by exact (intent, text) pair to avoid duplicates when an
        # industry pack reuses a default-style phrasing.
        seen: set = set()
        merged: list[PromptTemplate] = []
        for t in (*industry_templates, *default_templates):
            sig = (t.intent, t.text.strip().lower())
            if sig in seen:
                continue
            seen.add(sig)
            merged.append(t)
        return tuple(merged)
