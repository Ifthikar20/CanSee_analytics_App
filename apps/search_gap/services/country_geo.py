"""Countries, Google localization and engine methods for country checks.

Thin helpers so the views, the estimate and the runner never repeat
provider logic: the country catalog (the same list as the prompt location
picker), how Google is localized per country with the configured provider,
and how each engine is localized (``regions.planned_country_method``).
"""

from __future__ import annotations

from apps.citations.services import serp_google
from apps.llm_ranking.services import regions

GOOGLE = "google"
# Public engine keys in display order, and their backend provider keys.
ENGINE_KEYS = ("chatgpt", "claude", "gemini", "grok", "perplexity")
ENGINE_LABELS = {
    "chatgpt": "ChatGPT", "claude": "Claude", "gemini": "Gemini",
    "grok": "Grok", "perplexity": "Perplexity",
}
PROVIDER_LABELS = {
    serp_google.PROVIDER_SERPAPI: "SerpAPI",
    serp_google.PROVIDER_SCRAPERAPI: "ScraperAPI",
}


def provider_key(engine: str) -> str:
    """Backend provider key (PROVIDERS, AITokenUsage) of a public engine key."""
    return "gpt4" if engine == "chatgpt" else engine


def provider_class(engine: str):
    from apps.llm_ranking.providers import PROVIDERS

    return PROVIDERS.get(provider_key(engine))


def engine_configured(engine: str) -> bool:
    from apps.prompt_library.services.prompt_stats import is_configured

    cls = provider_class(engine)
    return cls is not None and is_configured(cls)


def catalog() -> list[dict]:
    """Every supported country, ``[{"country": "gb", "name": "United Kingdom"}]``,
    sorted by name."""
    return sorted(
        ({"country": c["code"].lower(), "name": c["name"]} for c in regions.supported_countries()),
        key=lambda c: c["name"],
    )


def country_names() -> dict[str, str]:
    return {c["country"]: c["name"] for c in catalog()}


def google_method(country: str, provider: str | None = None) -> dict | None:
    """``{method, label}`` for a Google search from ``country``; None when
    Google results are not configured (or ``provider`` is "")."""
    if provider is None:
        if not serp_google.is_configured():
            return None
        plan = serp_google.geo_plan(country)
    elif not provider:
        return None
    else:
        plan = serp_google.geo_plan(country, provider)
    return {"method": plan["method"], "label": plan["label"]}


def engine_options() -> list[dict]:
    """All five engines in display order, with how a country answer from
    each is localized and its planning upper bound per answer."""
    from apps.search_gap.services.country_checks import answer_usd_max, usd4

    out = []
    for key in ENGINE_KEYS:
        method = regions.planned_country_method(provider_key(key))
        out.append({
            "key": key,
            "label": ENGINE_LABELS[key],
            "configured": engine_configured(key),
            "method": method,
            "method_label": regions.engine_method_label(method, None),
            "usd_per_answer_max": usd4(answer_usd_max(key)),
        })
    return out
