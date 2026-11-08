"""
Competitor discovery service — finds real competitors using Google Search.

Falls back to LLM-based competitor discovery when available, but can
also work purely with Google Custom Search API.
"""

import logging
import re
from urllib.parse import urlparse

import requests
from django.conf import settings

from apps.citations.services.domain_classifier import _matches_suffix, is_aggregator

logger = logging.getLogger("apps")

SEARCH_TIMEOUT = 8  # seconds

# Social networks and forums the legacy filter also skipped, on top of the
# shared AGGREGATOR_DOMAINS.
_EXTRA_SKIP_DOMAINS = frozenset({
    "twitter.com", "x.com", "facebook.com", "instagram.com",
})


def discover_competitors(
    *,
    business_name: str,
    industry: str,
    domain: str,
    description: str = "",
    max_results: int = 8,
) -> list[dict]:
    """
    Discover real competitors using Google Search.

    Searches for "[business] competitors" and "[business] alternatives"
    to find real companies that compete in the same space.

    Returns:
        [{"name": str, "domain": str}, ...]
    """
    api_key = getattr(settings, "GOOGLE_SEARCH_API_KEY", "")
    cx = getattr(settings, "GOOGLE_SEARCH_ENGINE_ID", "")

    if not api_key or not cx:
        logger.info("Google Search not configured — skipping competitor discovery")
        return []

    # Build search queries
    queries = []
    if business_name:
        queries.append(f"{business_name} competitors")
        queries.append(f"{business_name} alternatives")
    if industry:
        queries.append(f"best {industry} tools")

    competitors = {}  # domain -> {name, domain}
    own_domain = _normalize_domain(domain) if domain else ""

    for query in queries[:2]:  # Limit to 2 API calls
        try:
            results = _google_search(api_key, cx, query)
            for item in results:
                comp = _extract_competitor(item, own_domain, business_name)
                if comp and comp["domain"] not in competitors:
                    competitors[comp["domain"]] = comp
                    if len(competitors) >= max_results:
                        break
        except Exception as e:
            logger.warning("Google search failed for '%s': %s", query, e)

    return list(competitors.values())[:max_results]


def _google_search(api_key: str, cx: str, query: str) -> list[dict]:
    """Execute a Google Custom Search API call."""
    resp = requests.get(
        "https://www.googleapis.com/customsearch/v1",
        params={
            "key": api_key,
            "cx": cx,
            "q": query,
            "num": 10,
        },
        timeout=SEARCH_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("items", [])


def _normalize_domain(domain: str) -> str:
    """Normalize a domain for comparison."""
    d = domain.lower().strip()
    d = re.sub(r"^(https?://)?", "", d)
    d = re.sub(r"^www\.", "", d)
    d = d.split("/")[0]
    return d


def _extract_competitor(
    item: dict, own_domain: str, own_name: str
) -> dict | None:
    """
    Extract a competitor from a Google search result.
    Filters out the business's own domain and non-competitor sites.
    """
    link = item.get("link", "")
    title = item.get("title", "")

    if not link:
        return None

    parsed = urlparse(link)
    domain = _normalize_domain(parsed.netloc)

    # Skip own domain (and its subdomains), by suffix: "acme.com" must not
    # drop "notacme.com".
    if own_domain and (domain == own_domain or domain.endswith("." + own_domain)):
        return None

    # Skip aggregator/review/social sites (not competitors). Suffix match:
    # a substring test let "x.com" drop dropbox.com and wix.com.
    if is_aggregator(domain) or _matches_suffix(domain, _EXTRA_SKIP_DOMAINS):
        return None

    # Skip if the title contains "review" or "comparison" (review articles)
    title_lower = title.lower()
    if any(w in title_lower for w in ["review", "comparison", "vs ", " vs.",
                                       "best ", "top ", "alternatives to"]):
        # These might be list articles, not competitor sites
        # But the link domain IS a competitor if it's not a review site
        pass

    # Extract a clean business name from the title
    name = _clean_title_to_company(title, domain)
    if not name or len(name) < 2:
        return None

    # Skip if it's our own company name
    if own_name and own_name.lower() in name.lower():
        return None

    return {"name": name, "domain": domain}


def _clean_title_to_company(title: str, domain: str) -> str:
    """Extract a company name from a search result title."""
    if not title:
        # Fall back to domain name
        parts = domain.split(".")
        return parts[0].capitalize() if parts else ""

    # Remove common suffixes
    clean = re.split(r"\s*[\|–—-]\s*", title)
    if clean:
        # The first part is usually the company name
        name = clean[0].strip()
        # Remove trailing descriptions like ": The Best Tool"
        name = re.split(r":\s+", name)[0].strip()
        if len(name) > 2:
            return name

    return domain.split(".")[0].capitalize()
