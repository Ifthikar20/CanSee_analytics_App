"""
Regions catalog for geo-segmented LLM ranking audits.

A region is a two-letter ISO country code plus a human label and the
country code passed to engines that accept a user location.

Selecting a region affects two places in the pipeline:

  1. The providers — Perplexity gets the region as
     ``web_search_options.user_location``; engines without a geo
     parameter (Gemini, Grok) get ``region_system_instruction``.
  2. ``citation_geo.attribute_country`` — labels each citation URL
     with the country derived from its ccTLD; the per-audit
     ``citation_countries`` aggregate is then a region-of-origin
     footprint for the brands that ranked.
"""
from __future__ import annotations

from dataclasses import dataclass

REGION_GLOBAL = "global"
REGION_US = "us"
REGION_CA = "ca"
REGION_IN = "in"
REGION_UK = "uk"
REGION_DE = "de"
REGION_AU = "au"


@dataclass(frozen=True)
class Region:
    code: str
    label: str
    perplexity_country: str  # ISO-2 code passed to Perplexity user_location


REGIONS: dict[str, Region] = {
    REGION_GLOBAL: Region(REGION_GLOBAL, "Global", ""),
    REGION_US: Region(REGION_US, "United States", "US"),
    REGION_CA: Region(REGION_CA, "Canada", "CA"),
    REGION_IN: Region(REGION_IN, "India", "IN"),
    REGION_UK: Region(REGION_UK, "United Kingdom", "GB"),
    REGION_DE: Region(REGION_DE, "Germany", "DE"),
    REGION_AU: Region(REGION_AU, "Australia", "AU"),
}

# Broad ISO-3166 country catalog, keyed by ISO-2 code (lower-case). Native
# geo (Perplexity / Claude / GPT user_location) uses ``perplexity_country``;
# models without a geo parameter (Gemini, Grok) instead get the location
# injected into their system instruction via :func:`region_system_instruction`.
_COUNTRY_NAMES = {
    "GB": "United Kingdom", "FR": "France", "ES": "Spain", "IT": "Italy",
    "NL": "Netherlands", "SE": "Sweden", "IE": "Ireland", "RU": "Russia",
    "UA": "Ukraine", "PL": "Poland", "BR": "Brazil", "MX": "Mexico",
    "AR": "Argentina", "CL": "Chile", "CO": "Colombia", "PE": "Peru",
    "JP": "Japan", "KR": "South Korea", "CN": "China", "TW": "Taiwan",
    "SG": "Singapore", "HK": "Hong Kong", "AE": "United Arab Emirates",
    "SA": "Saudi Arabia", "QA": "Qatar", "KW": "Kuwait", "BH": "Bahrain",
    "OM": "Oman", "JO": "Jordan", "LB": "Lebanon", "EG": "Egypt",
    "MA": "Morocco", "ZA": "South Africa", "NG": "Nigeria", "KE": "Kenya",
    "GH": "Ghana", "ID": "Indonesia", "PH": "Philippines", "MY": "Malaysia",
    "TH": "Thailand", "VN": "Vietnam", "PK": "Pakistan", "BD": "Bangladesh",
    "LK": "Sri Lanka", "NP": "Nepal", "TR": "Turkey", "IL": "Israel",
    "NZ": "New Zealand", "CH": "Switzerland", "AT": "Austria",
    "BE": "Belgium", "PT": "Portugal", "NO": "Norway", "DK": "Denmark",
    "FI": "Finland", "GR": "Greece", "CZ": "Czechia", "RO": "Romania",
    "HU": "Hungary", "BG": "Bulgaria", "HR": "Croatia", "SK": "Slovakia",
    "RS": "Serbia", "IS": "Iceland", "LU": "Luxembourg", "EE": "Estonia",
    "LV": "Latvia", "LT": "Lithuania",
}
for _iso, _label in _COUNTRY_NAMES.items():
    REGIONS.setdefault(_iso.lower(), Region(_iso.lower(), _label, _iso))

REGION_CHOICES = [(r.code, r.label) for r in REGIONS.values()]


def get_region(code: str) -> Region:
    """Lookup a region by code; falls back to GLOBAL for unknown codes."""
    return REGIONS.get((code or "").lower(), REGIONS[REGION_GLOBAL])


def region_for_country(code: str) -> str:
    """Map an ISO-2 country code to a supported region code (else GLOBAL).

    ``GB``/``UK`` both resolve to the British region. Any country present in
    the catalog routes to itself; unknown codes fall back to GLOBAL.
    """
    c = (code or "").strip().lower()
    if c == "uk":
        c = "gb"
    return c if c in REGIONS and c != REGION_GLOBAL else REGION_GLOBAL


def region_system_instruction(region_code: str) -> str:
    """A system-prompt sentence that localizes a model to the region.

    For providers without a native geo parameter (Gemini, Grok), and as a
    reinforcement for those that do, stating the user's location in the
    system instruction makes the model shift its frame of reference and its
    web-search sub-queries to that market. Empty for GLOBAL/unknown.
    """
    region = get_region(region_code)
    if not region or region.code == REGION_GLOBAL or not region.label:
        return ""
    country = region.label
    return (
        f"The user is located in {country}. Tailor every answer, "
        f"recommendation, brand, price, and local detail specifically to "
        f"{country}, focusing on services and options actually available "
        f"there. When you search the web, scope queries to {country}."
    )


def supported_countries() -> list[dict]:
    """Country options for the Add Prompt location picker.

    Returns ``[{"code": ISO-2, "name": label}]`` (no GLOBAL), de-duplicated
    by ISO-2 and sorted by name, so backend and frontend stay in sync.
    """
    by_iso: dict[str, str] = {}
    for r in REGIONS.values():
        iso = r.perplexity_country
        if iso:
            by_iso[iso] = r.label
    return sorted(
        ({"code": iso, "name": name} for iso, name in by_iso.items()),
        key=lambda c: c["name"],
    )

