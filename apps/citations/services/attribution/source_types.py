"""What kind of source a URL is, from the brand's point of view.

Owned pages are split by what they are (blog, terms, pricing, ...) from the
URL path; third-party pages reuse the tenant-free domain classes of
apps.citations.services.domain_classifier, plus a "review" class for review
and comparison directories. The labels are what the UI shows.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from apps.citations.models import SourceClass
from apps.citations.services.url_normalizer import extract_apex_domain

OWNER_YOU = "you"
OWNER_COMPETITOR = "competitor"
OWNER_THIRD_PARTY = "third_party"

# Own-site page types, tested in order against the URL path segments.
_OWN_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("terms", ("terms", "tos", "terms-of-service", "terms-and-conditions", "conditions", "eula")),
    ("privacy", ("privacy", "privacy-policy")),
    ("legal", ("legal", "cookie", "cookies", "cookie-policy", "dpa", "gdpr", "security",
               "compliance", "imprint", "accessibility")),
    ("pricing", ("pricing", "plans", "price", "prices")),
    ("blog", ("blog", "blogs", "articles", "article", "news", "posts", "post", "insights",
              "resources", "stories", "learn", "guides", "guide")),
    ("docs", ("docs", "documentation", "developers", "developer", "api", "reference", "kb",
              "knowledge-base", "manual")),
    ("support", ("help", "support", "faq", "faqs", "contact")),
    ("about", ("about", "about-us", "company", "team", "careers", "press")),
    ("product", ("product", "products", "features", "feature", "solutions", "platform",
                 "integrations", "services", "service", "shop", "store")),
)

OWN_LABELS = {
    "homepage": "Your homepage",
    "blog": "Your blog",
    "docs": "Your docs",
    "pricing": "Your pricing page",
    "product": "Your product page",
    "about": "Your about page",
    "terms": "Your terms page",
    "privacy": "Your privacy policy",
    "legal": "Your legal page",
    "support": "Your help pages",
    "page": "Your site",
}

THIRD_PARTY_LABELS = {
    "reddit": "Reddit",
    "review": "Review site",
    "forum": "Forum",
    "qa": "Q&A site",
    "news": "News",
    "wikipedia": "Wikipedia",
    "social": "Social media",
    "video": "Video",
    "blog": "Blog",
    "docs": "Documentation",
    "gov": "Government site",
    "edu": "Education site",
    "other": "Website",
}

COMPETITOR_LABEL = "Competitor site"

# Review and comparison directories (a subset of the classifier's
# AGGREGATOR_DOMAINS: the ones whose pages review or rate companies).
REVIEW_DOMAINS = frozenset({
    "g2.com", "capterra.com", "trustradius.com", "getapp.com", "softwareadvice.com",
    "producthunt.com", "alternativeto.net", "saasworthy.com", "sourceforge.net",
    "slant.co", "gartner.com", "crozdesk.com", "trustpilot.com", "clutch.co",
    "goodfirms.co", "yelp.com", "bbb.org", "sitejabber.com", "consumeraffairs.com",
    "tripadvisor.com", "glassdoor.com", "angi.com", "houzz.com", "healthgrades.com",
    "zocdoc.com", "avvo.com", "pcmag.com", "techradar.com",
})

_CLASS_TO_TYPE = {
    SourceClass.REDDIT: "reddit",
    SourceClass.QUORA: "qa",
    SourceClass.STACKOVERFLOW: "qa",
    SourceClass.NEWS: "news",
    SourceClass.WIKIPEDIA: "wikipedia",
    SourceClass.BLOG: "blog",
    SourceClass.FORUM: "forum",
    SourceClass.DOCS: "docs",
    SourceClass.SOCIAL: "social",
    SourceClass.YOUTUBE: "video",
    SourceClass.PODCAST: "video",
    SourceClass.GOV: "gov",
    SourceClass.EDU: "edu",
}


def _host(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def site_apex(website) -> str:
    """The apex domain of the website's own URL ("" when it has none)."""
    url = getattr(website, "url", "") or ""
    if not url:
        return ""
    if "://" not in url:
        url = f"https://{url}"
    return extract_apex_domain(_host(url))


def own_page_type(url: str) -> str:
    """The page type of an own-site URL from its path."""
    try:
        path = (urlsplit(url).path or "").lower()
    except ValueError:
        path = ""
    segments = [s for s in path.split("/") if s]
    if not segments:
        return "homepage"
    for kind, names in _OWN_RULES:
        for seg in segments:
            stem = seg.rsplit(".", 1)[0]  # terms.html
            if stem in names or any(stem.startswith(f"{n}-") for n in names):
                return kind
    return "page"


def _matches(host: str, domains) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def classify_source(url: str, *, website, competitor_apexes=()) -> dict:
    """``{"domain", "owner", "type", "type_label"}`` for ``url``."""
    host = _host(url)
    apex = extract_apex_domain(host) if host else ""
    own = site_apex(website)
    if apex and own and apex == own:
        kind = own_page_type(url)
        return {"domain": host, "owner": OWNER_YOU, "type": kind, "type_label": OWN_LABELS[kind]}
    comps = {c.lower().strip(".") for c in competitor_apexes or () if c}
    if apex and apex in comps:
        return {"domain": host, "owner": OWNER_COMPETITOR, "type": "competitor",
                "type_label": COMPETITOR_LABEL}
    if host and _matches(host, REVIEW_DOMAINS):
        kind = "review"
    else:
        from apps.citations.services.domain_classifier import classify

        try:
            source_class = classify(apex, host=host, persist=False)
        except Exception:
            source_class = SourceClass.OTHER
        kind = _CLASS_TO_TYPE.get(source_class, "other")
    return {"domain": host, "owner": OWNER_THIRD_PARTY, "type": kind,
            "type_label": THIRD_PARTY_LABELS[kind]}
