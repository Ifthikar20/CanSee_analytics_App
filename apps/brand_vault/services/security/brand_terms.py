"""The names that count as "the brand" for one website.

Used by the response auditor and by brand alignment to find the brand's
own statements inside an AI answer.
"""
from __future__ import annotations

from urllib.parse import urlparse

from apps.brand_vault.models import BrandSecurityConfig


def brand_terms(website) -> list[str]:
    """Resolve the brand terms to look for.

    Preference order: the website's BrandSecurityConfig terms, then the
    website's ``name``, then the root label of its domain. Empty strings
    are filtered out.
    """
    try:
        cfg = website.security_config
        if cfg and cfg.brand_terms:
            return [t for t in (str(x).strip() for x in cfg.brand_terms) if t]
    except BrandSecurityConfig.DoesNotExist:
        pass

    name = (website.name or "").strip()
    if name:
        return [name]
    if website.url:
        host = urlparse(website.url).netloc or website.url
        host = host.split(":", 1)[0]
        if host.startswith("www."):
            host = host[4:]
        root = host.split(".")[0] if "." in host else host
        return [root] if root else []
    return []
