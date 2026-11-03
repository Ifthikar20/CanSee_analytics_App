"""
LLM provider registry.

Each provider is a thin wrapper around an upstream SDK. The base class enforces
uniform token/cost recording, duration tracking, and error envelope so the
centralized usage rollup in Settings stays accurate by construction.

DeepSeek is intentionally NOT in :data:`PROVIDERS` — it must never be
selected by the audit router. Tools that explicitly want a cheap
synthesis provider should use :func:`get_synthesis_provider`, which
reads ``settings.PROMPT_SYNTHESIS_PROVIDER`` and falls back across the
configured tooling chain.
"""
from .base import LLMProvider, ProviderResult
from .claude import ClaudeProvider
from .deepseek import DeepSeekProvider
from .gemini import GeminiProvider
from .grok import GrokProvider
from .openai import OpenAIProvider
from .perplexity import PerplexityProvider

PROVIDERS: dict[str, type[LLMProvider]] = {
    "claude": ClaudeProvider,
    "gpt4": OpenAIProvider,
    "gemini": GeminiProvider,
    "perplexity": PerplexityProvider,
    "grok": GrokProvider,
}


# Selectable model variants per provider. Order matters — the first entry
# in each list is the default when only a provider key is supplied. Each
# variant tuple is (label, model_id, is_default).
#
# Adding a model here makes it selectable for a saved prompt. Removing one
# does not break historical runs (results store the model_id verbatim).
MODEL_VARIANTS: dict[str, list[tuple[str, str, bool]]] = {
    # Refreshed 2026-08-24 against each vendor's live /models listing
    # (OpenAI + Anthropic verified via API; Gemini/xAI dev keys are
    # invalid, so those entries follow the vendors' published catalogs).
    # Defaults match each provider class's default model so a prompt with
    # no explicit selection scans exactly as before.
    "claude": [
        ("Haiku 4.5",       "claude-haiku-4-5", True),
        ("Claude Sonnet 5", "claude-sonnet-5",  False),
        ("Claude Opus 5",   "claude-opus-5",    False),
        ("Claude Fable 5",  "claude-fable-5",   False),
    ],
    "gpt4": [
        ("GPT-4o mini",       "gpt-4o-mini",   True),
        ("GPT-5.6 Terra",     "gpt-5.6-terra", False),
        ("GPT-5.6 Sol",       "gpt-5.6-sol",   False),
        ("GPT-5.6 Luna",      "gpt-5.6-luna",  False),
        ("GPT-5.5",           "gpt-5.5",       False),
        ("GPT-4o",            "gpt-4o",        False),
    ],
    "gemini": [
        ("Gemini 2.5 Flash",       "gemini-2.5-flash",       True),
        ("Gemini 3.7 Flash",       "gemini-3.7-flash",       False),
        ("Gemini 3.5 Flash",       "gemini-3.5-flash",       False),
        ("Gemini 3.1 Pro Preview", "gemini-3.1-pro-preview", False),
    ],
    "perplexity": [
        ("Sonar (web)", "sonar", True),
        ("Sonar Pro (web)", "sonar-pro", False),
        ("Sonar Reasoning Pro", "sonar-reasoning-pro", False),
    ],
    "grok": [
        ("Grok 4",        "grok-4",        True),
        ("Grok 4.6",      "grok-4.6",      False),
        ("Grok 4.5",      "grok-4.5",      False),
        ("Grok 4.1 Fast", "grok-4.1-fast", False),
    ],
    # DeepSeek is excluded from the main audit registry (PROVIDERS) but
    # selectable for saved prompts because it's a cheap, useful
    # baseline for "did the model mention us" probes. The variants
    # registry, list_model_variants(), and get_provider_for_variant()
    # all consult TOOLING_PROVIDERS for these so the audit router
    # remains unaffected.
    "deepseek": [
        ("DeepSeek Chat",     "deepseek-chat",     True),
        ("DeepSeek Reasoner", "deepseek-reasoner", False),
    ],
}


def list_model_variants() -> list[dict]:
    """Return a flat list of variants for the picker UI.

    Each entry includes the provider key, the canonical variant id (used
    as the wire-format choice in BrandPrompt.model_variants), the model id sent to
    the SDK, a friendly label, and whether the underlying provider is
    configured. Variants are excluded when the provider class isn't in
    either PROVIDERS or TOOLING_PROVIDERS (defensive — should not
    happen). DeepSeek is sourced from TOOLING_PROVIDERS so it appears
    in the picker but stays out of the main audit router.
    """
    from django.conf import settings
    out: list[dict] = []
    for provider_key, variants in MODEL_VARIANTS.items():
        cls = PROVIDERS.get(provider_key) or TOOLING_PROVIDERS.get(provider_key)
        if cls is None:
            continue
        configured = bool(getattr(settings, cls.api_key_setting, ""))
        for label, model_id, is_default in variants:
            out.append({
                "id": f"{provider_key}:{model_id}",
                "provider": provider_key,
                "model_id": model_id,
                "label": label,
                "is_default": is_default,
                "configured": configured,
            })
    return out


def parse_variant(variant_id: str) -> tuple[str, str] | None:
    """Split `"<provider>:<model_id>"` into (provider, model_id).

    Returns None when the variant is malformed or references a provider
    that no longer exists. The caller should treat None as an unknown
    variant and surface a clear error to the user. Accepts any provider
    that's in PROVIDERS *or* TOOLING_PROVIDERS so a prompt scan can route
    to DeepSeek (which is tooling-only).
    """
    if not variant_id or ":" not in variant_id:
        return None
    provider, model_id = variant_id.split(":", 1)
    provider = provider.strip().lower()
    model_id = model_id.strip()
    if not provider or not model_id:
        return None
    if provider not in PROVIDERS and provider not in TOOLING_PROVIDERS:
        return None
    return provider, model_id


def get_provider_for_variant(variant_id: str) -> LLMProvider | None:
    """Instantiate a configured provider bound to the variant's model id.

    Mutates the instance's `.model` so the upstream SDK call routes to
    the requested variant. Returns None when the provider is unknown,
    the variant is malformed, or the API key isn't configured. Looks
    up the class in PROVIDERS first, then TOOLING_PROVIDERS, so a prompt
    scan can dispatch DeepSeek without polluting the audit router.
    """
    parsed = parse_variant(variant_id)
    if parsed is None:
        return None
    provider_key, model_id = parsed
    cls = PROVIDERS.get(provider_key) or TOOLING_PROVIDERS.get(provider_key)
    if cls is None:
        return None
    instance = cls()
    if not instance.is_configured():
        return None
    instance.model = model_id
    return instance

# Tooling-only registry. Intentionally separate from ``PROVIDERS`` so a
# typo in audit code can't accidentally route a real audit through
# DeepSeek (which lacks citation metadata, web grounding, etc).
TOOLING_PROVIDERS: dict[str, type[LLMProvider]] = {
    "deepseek": DeepSeekProvider,
    "claude": ClaudeProvider,
    "gpt4": OpenAIProvider,
}


def get_provider(key: str) -> LLMProvider | None:
    """Instantiate a provider by audit-side key, or return None if not configured."""
    cls = PROVIDERS.get(key)
    if cls is None:
        return None
    instance = cls()
    return instance if instance.is_configured() else None


def get_synthesis_provider(key: str | None = None) -> LLMProvider | None:
    """Return a configured tooling provider for synthesis / auto-templating.

    Falls back across the configured choice -> deepseek -> claude -> gpt4
    so callers always get a usable provider when at least one key is set.
    Returns ``None`` only when no tooling provider has an API key — every
    caller must handle that case gracefully.
    """
    from django.conf import settings

    chain: list[str] = []
    chosen = key or getattr(settings, "PROMPT_SYNTHESIS_PROVIDER", "deepseek")
    chain.append(chosen)
    for fallback in ("deepseek", "claude", "gpt4"):
        if fallback not in chain:
            chain.append(fallback)
    for name in chain:
        cls = TOOLING_PROVIDERS.get(name)
        if cls is None:
            continue
        instance = cls()
        if instance.is_configured():
            return instance
    return None


__all__ = [
    "PROVIDERS",
    "TOOLING_PROVIDERS",
    "get_provider",
    "get_synthesis_provider",
    "LLMProvider",
    "ProviderResult",
    "ClaudeProvider",
    "OpenAIProvider",
    "GeminiProvider",
    "PerplexityProvider",
    "GrokProvider",
    "DeepSeekProvider",
]
