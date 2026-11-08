"""Domain -> SourceClass classifier.

Rule-based first pass, with a persisted cache (DomainClassification) so
hot domains never re-evaluate the rule list. Per-tenant overrides for
``your_site`` and ``competitor_site`` short-circuit the cache because they
depend on the calling website.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from urllib.parse import urlsplit

from apps.citations.models import DomainClassification, SourceClass
from apps.citations.services.url_normalizer import extract_apex_domain

logger = logging.getLogger("apps")


# Curated starter list of news domains. The classifier is deliberately
# conservative — when in doubt fall through to OTHER and let the LLM
# classifier upgrade it later.
_NEWS_DOMAINS = {
    "techcrunch.com", "theverge.com", "nytimes.com", "bbc.com", "bbc.co.uk",
    "reuters.com", "bloomberg.com", "wsj.com", "cnbc.com", "ft.com",
    "theguardian.com", "arstechnica.com", "wired.com", "engadget.com",
    "theatlantic.com", "businessinsider.com", "cnn.com", "npr.org",
    "washingtonpost.com", "axios.com", "vox.com", "politico.com",
    "forbes.com", "fortune.com", "economist.com",
}

_BLOG_DOMAINS = {
    "medium.com", "substack.com", "dev.to", "hashnode.dev",
    "wordpress.com", "blogger.com", "ghost.io", "tumblr.com",
}

_SOCIAL_DOMAINS = {
    "twitter.com", "x.com", "linkedin.com", "facebook.com",
    "instagram.com", "tiktok.com", "mastodon.social", "threads.net",
    "pinterest.com", "snapchat.com",
}

_FORUM_DOMAINS = {
    "ycombinator.com",  # news.ycombinator.com handled via subdomain rule too
    "slashdot.org", "indiehackers.com",
}

_PODCAST_HOST_HINTS = ("spotify.com", "apple.com", "podcasts.apple.com",
                       "podbean.com", "buzzsprout.com")


# Sites that list, review or discuss companies rather than being one: review
# and comparison directories, search engines, marketplaces and big
# publishers. A Google result on one of these is never itself a competitor.
# Matched by domain suffix (``is_aggregator``), so "x.com" never matches
# "dropbox.com".
AGGREGATOR_DOMAINS = frozenset({
    "g2.com", "capterra.com", "trustradius.com", "getapp.com", "softwareadvice.com",
    "producthunt.com", "alternativeto.net", "saasworthy.com", "sourceforge.net",
    "slant.co", "gartner.com", "crozdesk.com", "trustpilot.com", "clutch.co",
    "goodfirms.co", "yelp.com", "bbb.org", "crunchbase.com", "zapier.com",
    "techradar.com", "pcmag.com", "reddit.com", "quora.com", "youtube.com",
    "github.com", "linkedin.com", "medium.com", "wikipedia.org", "amazon.com",
    "bing.com", "yahoo.com", "duckduckgo.com", "apps.apple.com", "play.google.com",
    "slashdot.org", "stackoverflow.com", "techcrunch.com", "forbes.com",
    "bloomberg.com", "businessinsider.com",
})


def _matches_suffix(host: str, suffixes: Iterable[str]) -> bool:
    return any(host == s or host.endswith("." + s) for s in suffixes)


def is_aggregator(host: str) -> bool:
    """True when ``host`` belongs to a directory, review site, search engine
    or marketplace (``AGGREGATOR_DOMAINS``, or any google.* domain)."""
    host = (host or "").lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return False
    if host.split(".")[0] == "google" or ".google." in f".{host}":
        return True
    return _matches_suffix(host, AGGREGATOR_DOMAINS)


def _rule_classify(apex: str, host: str) -> str:
    """Pure-function rules. Return a SourceClass value or OTHER."""
    if not apex:
        return SourceClass.OTHER

    if _matches_suffix(host, ["reddit.com"]) or apex == "reddit.com":
        return SourceClass.REDDIT
    if _matches_suffix(host, ["quora.com"]) or apex == "quora.com":
        return SourceClass.QUORA
    if apex == "stackoverflow.com" or apex.endswith(".stackexchange.com") or apex == "stackexchange.com":
        return SourceClass.STACKOVERFLOW
    if _matches_suffix(host, ["wikipedia.org"]) or apex.endswith("wikipedia.org"):
        return SourceClass.WIKIPEDIA
    if _matches_suffix(host, ["youtube.com"]) or apex == "youtu.be" or apex == "youtube.com":
        return SourceClass.YOUTUBE

    # Government / education TLDs
    if host.endswith(".gov") or ".gov." in "." + host:
        return SourceClass.GOV
    if host.endswith(".edu") or host.endswith(".ac.uk") or ".edu." in "." + host:
        return SourceClass.EDU

    if apex in _SOCIAL_DOMAINS or _matches_suffix(host, _SOCIAL_DOMAINS):
        return SourceClass.SOCIAL
    if apex in _BLOG_DOMAINS or _matches_suffix(host, _BLOG_DOMAINS):
        return SourceClass.BLOG
    if apex in _NEWS_DOMAINS or _matches_suffix(host, _NEWS_DOMAINS):
        return SourceClass.NEWS

    # Podcast platforms
    if any(h in host for h in _PODCAST_HOST_HINTS):
        # Spotify/Apple are also social/store; require a path hint at call sites
        # for a tighter classification. Fall through if no match.
        if "podcast" in host or host.startswith("podcasts."):
            return SourceClass.PODCAST

    # Subdomain heuristics
    if host.startswith("docs.") or host.startswith("developer.") or host.startswith("developers."):
        return SourceClass.DOCS
    if host.startswith("blog."):
        return SourceClass.BLOG
    if host.startswith("community.") or host.startswith("forum.") or host.startswith("forums."):
        return SourceClass.FORUM
    if host.startswith("discourse."):
        return SourceClass.FORUM
    if host.startswith("news."):
        return SourceClass.NEWS

    if apex in _FORUM_DOMAINS:
        return SourceClass.FORUM

    return SourceClass.OTHER


def classify(
    apex_domain: str,
    *,
    host: str | None = None,
    website=None,
    competitor_apexes: Iterable[str] | None = None,
    persist: bool = True,
) -> str:
    """Classify ``apex_domain`` into a :class:`SourceClass` value.

    Per-tenant overrides (``your_site`` / ``competitor_site``) are applied
    *before* the cache so that two tenants citing the same blog never see
    each other's labels.
    """
    if not apex_domain:
        return SourceClass.OTHER

    apex = apex_domain.lower().strip(".")
    h = (host or apex).lower().strip(".")

    # Per-tenant: target site
    if website is not None:
        site_url = getattr(website, "url", "") or ""
        if site_url:
            try:
                site_host = urlsplit(site_url).netloc or site_url
                site_apex = extract_apex_domain(site_host)
                if site_apex and site_apex == apex:
                    return SourceClass.YOUR_SITE
            except Exception:
                pass

    # Per-tenant: competitor list
    if competitor_apexes:
        comp_set = {c.lower().strip(".") for c in competitor_apexes if c}
        if apex in comp_set:
            return SourceClass.COMPETITOR_SITE

    # Cached lookup
    cached = DomainClassification.objects.filter(apex_domain=apex).first()
    if cached:
        return cached.source_class

    label = _rule_classify(apex, h)
    if persist:
        try:
            DomainClassification.objects.update_or_create(
                apex_domain=apex,
                defaults={"source_class": label, "classified_by": "rules"},
            )
        except Exception as exc:  # pragma: no cover
            logger.debug("Failed to cache domain classification for %s: %s", apex, exc)
    return label


def collect_competitor_apexes(website) -> set[str]:
    """Return the set of competitor apex domains known for ``website``.

    Reads from any LLMRankingResult.competitors_mentioned aggregates that
    carry a ``url`` field, plus competitor names matched against citations
    is left to a future enhancement. Resilient to a website without audits.
    """
    out: set[str] = set()
    if website is None:
        return out
    try:
        from apps.llm_ranking.models import LLMRankingResult
        results = LLMRankingResult.objects.filter(
            audit__website=website
        ).values_list("competitors_mentioned", flat=True)[:500]
        for entries in results:
            if not entries:
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                url = entry.get("url") or entry.get("link") or ""
                if not url:
                    continue
                try:
                    host = urlsplit(url).netloc or url
                    apex = extract_apex_domain(host)
                    if apex:
                        out.add(apex)
                except Exception:
                    continue
    except Exception:
        return out
    return out
