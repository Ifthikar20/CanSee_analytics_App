"""AI crawler requests from Cloudflare (pages contract 4e, "crawlers").

Crawlers do not run JavaScript, so neither the Cansee pixel nor GA4 ever
sees them; Cloudflare sees every request to a proxied zone. The report
groups the zone's adaptive request data by user agent for the last 7 UTC
days (free zones keep about 8 days and allow 1-day queries, so it is one
request per day), keeps the user agents of known AI crawlers and
un-samples the counts with cloudflare_client._estimate.

Honesty notes: the counts are sampled estimates, and user agents can be
faked (verified-bot detection needs Bot Management), so a request that
calls itself GPTBot is counted as GPTBot.

Whether the zone's plan allows the user-agent query is learned from the
first request and remembered for a day per zone (``capability``); the
traffic sources payload shows it as measures.ai_crawlers.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import UTC, datetime, time, timedelta

from django.core.cache import cache
from django.utils import timezone

from apps.web_analytics.services import cloudflare_client, snapshots

logger = logging.getLogger("apps")

WINDOW_DAYS = 7
CAPABILITY_TTL_SECONDS = 86400
REPORT_TTL_SECONDS = 1800
# The AI traffic page shows the first 10; Pages (insights) reads them all.
MAX_PATHS = 50
_TIME_FMT = "%Y-%m-%dT%H:%M:%SZ"

# (key, user-agent token, name, operator key, operator, purpose). The
# token is spelled exactly as the crawler sends it: Cloudflare's
# userAgent_like is case-sensitive (Meta's agents send lowercase names).
CATALOG = (
    ("gptbot", "GPTBot", "GPTBot", "openai", "OpenAI", "training"),
    ("oai-searchbot", "OAI-SearchBot", "OAI-SearchBot", "openai", "OpenAI", "search"),
    ("chatgpt-user", "ChatGPT-User", "ChatGPT-User", "openai", "OpenAI", "user"),
    ("claudebot", "ClaudeBot", "ClaudeBot", "anthropic", "Anthropic", "training"),
    ("claude-searchbot", "Claude-SearchBot", "Claude-SearchBot", "anthropic", "Anthropic",
     "search"),
    ("claude-user", "Claude-User", "Claude-User", "anthropic", "Anthropic", "user"),
    ("anthropic-ai", "anthropic-ai", "anthropic-ai", "anthropic", "Anthropic", "training"),
    ("perplexitybot", "PerplexityBot", "PerplexityBot", "perplexity", "Perplexity", "search"),
    ("perplexity-user", "Perplexity-User", "Perplexity-User", "perplexity", "Perplexity", "user"),
    ("ccbot", "CCBot", "CCBot", "commoncrawl", "Common Crawl", "training"),
    ("bytespider", "Bytespider", "Bytespider", "bytedance", "ByteDance", "training"),
    ("amazonbot", "Amazonbot", "Amazonbot", "amazon", "Amazon", "search"),
    ("meta-externalagent", "meta-externalagent", "Meta-ExternalAgent", "meta", "Meta", "training"),
    ("meta-externalfetcher", "meta-externalfetcher", "Meta-ExternalFetcher", "meta", "Meta",
     "user"),
    ("cohere-ai", "cohere-ai", "cohere-ai", "cohere", "Cohere", "training"),
    ("mistralai-user", "MistralAI-User", "MistralAI-User", "mistral", "Mistral", "user"),
    ("duckassistbot", "DuckAssistBot", "DuckAssistBot", "duckduckgo", "DuckDuckGo", "search"),
    ("youbot", "YouBot", "YouBot", "you", "You.com", "search"),
    ("applebot", "Applebot", "Applebot", "apple", "Apple", "search"),
)
_META = {key: (name, operator, purpose) for key, _t, name, _ok, operator, purpose in CATALOG}
# Most specific first: a token that contains another is tried before it.
_TOKENS = sorted(((token.casefold(), key) for key, token, *_rest in CATALOG),
                 key=lambda pair: -len(pair[0]))

PURPOSES = (
    ("training", "Model training"),
    ("search", "AI search index"),
    ("user", "Fetches for a user's question"),
    ("other", "Other"),
)
NOTES = ("Counted from user-agent strings, which bots can fake. Cloudflare samples these numbers.",)
STALE_NOTE = "Showing the last saved copy; Cloudflare could not be reached."

# GraphQL error text that means the zone's plan refused the query (as
# opposed to Cloudflare failing): access or permission errors, and
# fields or arguments the plan's schema does not have.
_REFUSED = (
    "access", "permission", "authoriz", "unknown field", "unknown argument", "unknown arg",
    "cannot query field",
)

_QUERY = """
query ($zone: String!, $since: Time!, $until: Time!) {
  viewer { zones(filter: {zoneTag: $zone}) {
    agents: httpRequestsAdaptiveGroups(limit: 200, orderBy: [count_DESC],
      filter: {datetime_geq: $since, datetime_lt: $until, requestSource: "eyeball", OR: [%(or)s]}) {
      count
      avg { sampleInterval }
      dimensions { userAgent }
    }
    paths: httpRequestsAdaptiveGroups(limit: 100, orderBy: [count_DESC],
      filter: {datetime_geq: $since, datetime_lt: $until, requestSource: "eyeball", OR: [%(or)s]}) {
      count
      avg { sampleInterval }
      dimensions { clientRequestPath userAgent }
    }
  } }
}
"""


def patterns() -> list[str]:
    """userAgent_like patterns, in catalog order."""
    return [f"%{token}%" for _key, token, *_rest in CATALOG]


def query() -> str:
    """The per-day GraphQL document (static text; no user input)."""
    clause = ", ".join(f'{{userAgent_like: "{pattern}"}}' for pattern in patterns())
    return _QUERY % {"or": clause}


def classify(user_agent: str) -> str | None:
    """Catalog key of a user agent (case-insensitive), or None."""
    agent = (user_agent or "").casefold()
    for token, key in _TOKENS:
        if token in agent:
            return key
    return None


def _capability_key(zone_id: str) -> str:
    return f"wa:cfcrawl:{zone_id}"


def capability(zone_id) -> bool | str:
    """True or False once a query showed whether this zone's plan allows
    the user-agent query (remembered a day); "unknown" before that."""
    if not zone_id:
        return "unknown"
    try:
        value = cache.get(_capability_key(zone_id))
    except Exception:
        value = None
    if value == "supported":
        return True
    if value == "unsupported":
        return False
    return "unknown"


def _remember(zone_id: str, supported: bool) -> None:
    try:
        cache.set(_capability_key(zone_id), "supported" if supported else "unsupported",
                  CAPABILITY_TTL_SECONDS)
    except Exception:
        pass


def _refused(errors: list) -> bool:
    text = " ".join(errors).casefold()
    return any(marker in text for marker in _REFUSED)


def unavailable(reason: str) -> dict:
    """The crawlers payload when nothing can be shown."""
    return {
        "source": "cloudflare",
        "available": False,
        "reason": reason,
        "window_days": WINDOW_DAYS,
        "window_start": None,
        "sampled": True,
        "verified": False,
        "total_requests": None,
        "bots": [],
        "purposes": [],
        "series": {"labels": [], "by_bot": {}},
        "paths": [],
        "notes": [],
    }


def _days(now: datetime) -> list[tuple]:
    """(day, since, until) for the last WINDOW_DAYS UTC days, oldest first;
    today ends now."""
    today = now.date()
    out = []
    for offset in range(WINDOW_DAYS - 1, -1, -1):
        day = today - timedelta(days=offset)
        since = datetime.combine(day, time.min, tzinfo=UTC)
        out.append((day, since, min(since + timedelta(days=1), now)))
    return out


def _pct(part: int, whole: int) -> float | None:
    return round(100 * part / whole, 1) if whole else None


def _fetch(api_token: str, zone_id: str) -> dict | None:
    """The report, plan_unsupported, or None when Cloudflare failed."""
    days = _days(timezone.now())
    document = query()
    per_day: dict = {}
    paths: dict = {}
    # Newest day first: it is always inside the zone's retention, so its
    # answer decides whether the plan allows the query at all.
    for probe, (day, since, until) in enumerate(reversed(days)):
        errors: list = []
        variables = {
            "zone": zone_id,
            "since": since.strftime(_TIME_FMT),
            "until": until.strftime(_TIME_FMT),
        }
        data = cloudflare_client._post_graphql(api_token, document, variables, errors=errors)
        if data is None:
            if probe == 0 and _refused(errors):
                _remember(zone_id, False)
                return unavailable("plan_unsupported")
            return None
        zones = (data.get("viewer") or {}).get("zones") or []
        if not zones:
            logger.warning("Cloudflare crawler query returned no zone for %s", zone_id)
            return None
        if probe == 0:
            _remember(zone_id, True)
        zone = zones[0]

        counts: Counter = Counter()
        for group in zone.get("agents") or []:
            key = classify((group.get("dimensions") or {}).get("userAgent", ""))
            if key is not None:
                counts[key] += cloudflare_client._estimate(group)["requests"]
        per_day[day] = counts
        for group in zone.get("paths") or []:
            dimensions = group.get("dimensions") or {}
            key = classify(dimensions.get("userAgent", ""))
            path = dimensions.get("clientRequestPath") or ""
            if key is None or not path:
                continue
            entry = paths.setdefault(path, {"requests": 0, "bots": set()})
            entry["requests"] += cloudflare_client._estimate(group)["requests"]
            entry["bots"].add(key)

    totals: Counter = Counter()
    for counts in per_day.values():
        totals.update(counts)
    total = sum(totals.values())
    seen = sorted((key for key in totals if totals[key] > 0), key=lambda key: (-totals[key], key))
    by_purpose: Counter = Counter()
    for key in seen:
        by_purpose[_META[key][2]] += totals[key]
    labels = [day for day, _since, _until in days]
    catalog_order = [key for key, *_rest in CATALOG]
    top_paths = sorted(paths.items(), key=lambda item: (-item[1]["requests"], item[0]))[:MAX_PATHS]
    return {
        "source": "cloudflare",
        "available": True,
        "reason": None,
        "window_days": WINDOW_DAYS,
        "window_start": labels[0].isoformat(),
        "sampled": True,
        "verified": False,
        "total_requests": total,
        "bots": [
            {
                "key": key,
                "name": _META[key][0],
                "operator": _META[key][1],
                "purpose": _META[key][2],
                "requests": totals[key],
                "share_pct": _pct(totals[key], total),
            }
            for key in seen
        ],
        "purposes": [
            {"key": key, "label": label, "requests": by_purpose[key]}
            for key, label in PURPOSES
            if by_purpose[key]
        ],
        "series": {
            "labels": [day.isoformat() for day in labels],
            "by_bot": {key: [per_day[day].get(key, 0) for day in labels] for key in seen},
        },
        "paths": [
            {
                "path": path,
                "requests": entry["requests"],
                "bots": [key for key in catalog_order if key in entry["bots"]],
            }
            for path, entry in top_paths
        ],
        "notes": list(NOTES),
    }


def crawler_report(integration, website) -> dict:
    """The crawlers part of AiTraffic for a connected Cloudflare zone
    (cached 30 minutes per website and zone)."""
    metadata = integration.metadata or {}
    zone_id = metadata.get("zone_id")
    api_token = integration.access_token
    if not zone_id or not api_token:
        return unavailable("no_cloudflare")

    def fetch():
        if capability(zone_id) is False:
            return unavailable("plan_unsupported")
        return _fetch(api_token, zone_id)

    payload = snapshots.get_or_fetch(f"cfai:{zone_id}", website.id, fetch, ttl=REPORT_TTL_SECONDS)
    if payload.get("pending"):
        return unavailable("source_error")
    report = {key: value for key, value in payload.items() if key != "stale"}
    if payload.get("stale") and report.get("available"):
        report["notes"] = [*report.get("notes", []), STALE_NOTE]
    return report
