"""
Real Google results for the onboarding scan: competitors and buyer questions.

The scan asks Google, through the app's Google results provider
(``apps.citations.services.serp_google``: SerpAPI or ScraperAPI, whichever
is configured), up to three questions:

    "<brand> alternatives"   competitors (and People also ask / related)
    "<brand> competitors"    competitors (and People also ask / related)
    "best <industry>"        buyer questions only

The searches run on worker threads while the scan's Claude passes run, each
with a short timeout (``ONBOARDING_SERP_TIMEOUT``), and every search counts
against a per-user daily limit (``ONBOARDING_SERP_DAILY_SEARCHES``). Nothing
here touches the database, so the threads need no connection of their own.

Competitors are picked only from what Google returned:

- the site itself, review directories, search engines, marketplaces,
  publishers and social sites are dropped (``domain_classifier``);
- with Claude configured, Claude chooses among the numbered results and every
  pick is checked against the row it cites (source "both");
- otherwise, or when Claude picks nothing, the remaining result sites are
  suggested, most frequent first (source "search").

A brand search Google answered for a different spelling (fewer than two
results name the brand) is not used at all.

Every function returns well-formed empty values on failure; nothing raises.
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import re
from dataclasses import dataclass, field

from django.conf import settings

from core.quota import DailyQuota

logger = logging.getLogger("apps")

PURPOSE_COMPETITORS = "competitors"
PURPOSE_QUESTIONS = "questions"

# Where a suggestion came from, as the scan payload reports it.
SOURCE_SEARCH = "search"   # Google's results alone
SOURCE_AI = "ai"           # Claude alone
SOURCE_BOTH = "both"       # Google's results, chosen or worded by Claude

BRAND_MAX = 100            # characters of the brand name sent anywhere
BRAND_MIN = 3              # shorter names make brand searches meaningless
COMPETITORS_MAX = 8
PROMPT_ROWS_MAX = 20
QUESTIONS_INPUT_MAX = 12
RELATED_INPUT_MAX = 12
# A brand search is trusted when at least this many results name the brand.
GUARD_MIN_ROWS = 2
RESULTS_PER_QUERY = 10


def search_quota() -> DailyQuota:
    """Onboarding Google searches per user per UTC day."""
    return DailyQuota(
        namespace="onboarding_serp",
        setting_name="ONBOARDING_SERP_DAILY_SEARCHES",
        default_limit=15,
    )


def _timeout() -> float:
    return float(getattr(settings, "ONBOARDING_SERP_TIMEOUT", 20.0) or 20.0)


def fold(text: object) -> str:
    """Comparison form: sanitized, lowercase, single spaces."""
    from core.text_sanitizer import sanitize_untrusted_text

    return re.sub(r"\s+", " ", sanitize_untrusted_text(str(text or ""))).strip().lower()


def clean_text(text, limit: int = 300) -> str:
    """Untrusted result text made safe to show and to put in a prompt."""
    from core.text_sanitizer import sanitize_untrusted_text

    return re.sub(r"\s+", " ", sanitize_untrusted_text(str(text or ""))).strip()[:limit]


def whole_words(needle: str, haystack: str) -> bool:
    """True when folded ``needle`` occurs in folded ``haystack`` as whole words."""
    needle, haystack = fold(needle), fold(haystack)
    if not needle or not haystack:
        return False
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack) is not None


def apex_of(host: str) -> str:
    from apps.citations.services.url_normalizer import extract_apex_domain

    host = (host or "").lower().strip().strip(".")
    host = re.sub(r"^https?://", "", host).split("/")[0]
    return extract_apex_domain(host.removeprefix("www."))


def _label(domain: str) -> str:
    """The first label of a domain with hyphens removed: "my-app.io" -> "myapp"."""
    return (domain or "").split(".")[0].replace("-", "")


def _name_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", fold(name))


@dataclass
class QueryResult:
    query: str
    purpose: str
    payload: dict = field(default_factory=dict)
    error: str = ""
    # False when the results name a different brand (Google corrected the
    # spelling); such a search is reported but not used.
    trusted: bool = True

    @property
    def organic(self) -> list[dict]:
        return list(self.payload.get("organic") or []) if self.usable else []

    @property
    def usable(self) -> bool:
        return not self.error and self.trusted

    def as_dict(self) -> dict:
        return {
            "query": self.query,
            "purpose": self.purpose,
            "error": self.error,
            "results": len(self.payload.get("organic") or []) if not self.error else 0,
        }


@dataclass
class GoogleResults:
    """What the onboarding scan learned from Google."""

    configured: bool = False
    provider: str = ""
    queries: list[QueryResult] = field(default_factory=list)
    searches: int = 0
    brand: str = ""

    @property
    def label(self) -> str:
        from apps.citations.services import serp_google

        return serp_google.PROVIDER_LABELS.get(self.provider, "")

    @property
    def error(self) -> str:
        """"" when any search gave usable results, else the first error."""
        if not self.configured:
            return "serpapi_not_configured"
        if not self.queries:
            return ""
        if any(q.usable for q in self.queries):
            return ""
        for q in self.queries:
            if q.error:
                return q.error
        return "no_brand_results"

    def competitor_queries(self) -> list[QueryResult]:
        return [q for q in self.queries if q.purpose == PURPOSE_COMPETITORS and q.usable]

    def has_competitor_results(self) -> bool:
        return any(q.organic for q in self.competitor_queries())

    def _ordered(self) -> list[QueryResult]:
        # Buyer questions first: the brand searches mostly ask about the brand.
        return ([q for q in self.queries if q.purpose == PURPOSE_QUESTIONS and q.usable]
                + self.competitor_queries())

    def _texts(self, key: str, limit: int) -> list[dict]:
        seen: set[str] = set()
        out: list[dict] = []
        for q in self._ordered():
            for raw in q.payload.get(key) or []:
                text = raw.get("question") if isinstance(raw, dict) else raw
                text = clean_text(text, 300)
                folded = fold(text)
                if len(text) < 6 or folded in seen:
                    continue
                if self.brand and whole_words(self.brand, text):
                    continue  # about the brand itself, not what buyers look for
                seen.add(folded)
                out.append({"text": text, "query": q.query})
                if len(out) >= limit:
                    return out
        return out

    def questions(self) -> list[dict]:
        """People also ask, as ``[{text, query}]``, without the brand's own name."""
        return self._texts("questions", QUESTIONS_INPUT_MAX)

    def related(self) -> list[dict]:
        """Related searches, as ``[{text, query}]``, without the brand's own name."""
        return self._texts("related_searches", RELATED_INPUT_MAX)

    def block(self) -> dict:
        """The scan payload's ``google`` field."""
        return {
            "configured": self.configured,
            "provider": self.provider,
            "label": self.label,
            "error": self.error,
            "searches": self.searches,
            "queries": [q.as_dict() for q in self.queries],
        }


def _brand_guard(result: QueryResult, brand: str) -> bool:
    """True when enough results name ``brand``: otherwise Google answered for
    another spelling and the results are about someone else."""
    hits = 0
    for row in result.payload.get("organic") or []:
        if whole_words(brand, f"{row.get('title', '')} {row.get('snippet', '')}"):
            hits += 1
            if hits >= GUARD_MIN_ROWS:
                return True
    return False


class GoogleLookup:
    """Google searches for one onboarding scan, run on worker threads.

    ``start`` submits the searches straight away so they overlap the scan's
    Claude passes; ``collect`` waits for them (bounded by the timeout) and
    returns ``GoogleResults``.
    """

    def __init__(self, *, business_name: str, user=None):
        from apps.citations.services import serp_google

        self.brand = clean_text(business_name, BRAND_MAX)
        self.user_key = getattr(user, "pk", None)
        self.provider = serp_google.provider()
        self.timeout = _timeout()
        self.searches = 0
        self._pending: list[tuple[str, str, concurrent.futures.Future | None, str]] = []
        self._queries: set[str] = set()
        self._executor: concurrent.futures.ThreadPoolExecutor | None = None
        self.has_seed = False

    @classmethod
    def start(cls, *, business_name: str, seed: str = "", user=None) -> GoogleLookup:
        lookup = cls(business_name=business_name, user=user)
        if not lookup.provider:
            return lookup
        if len(lookup.brand) >= BRAND_MIN:
            lookup.submit(f"{lookup.brand} alternatives", PURPOSE_COMPETITORS)
            lookup.submit(f"{lookup.brand} competitors", PURPOSE_COMPETITORS)
        lookup.submit_seed(seed)
        return lookup

    def submit_seed(self, seed: str) -> None:
        """The buyer-question search, "best <industry or first offering>"."""
        seed = clean_text(seed, 100)
        if self.has_seed or not seed or not self.provider:
            return
        self.has_seed = True
        topic = seed if seed.lower().startswith("best ") else f"best {seed}"
        self.submit(topic, PURPOSE_QUESTIONS)

    def submit(self, query: str, purpose: str) -> None:
        from apps.citations.services import serp_google

        key = fold(query)
        if not key or key in self._queries:
            return
        self._queries.add(key)
        try:
            allowed = search_quota().consume(self.user_key)
        except Exception:
            # No counter, no search: the limit is never skipped.
            logger.warning("onboarding: Google search limit unavailable", exc_info=True)
            self._pending.append((query, purpose, None, "limit_unavailable"))
            return
        if not allowed:
            self._pending.append((query, purpose, None, "daily_limit"))
            return
        if self._executor is None:
            self._executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=3, thread_name_prefix="onboarding-serp",
            )
        self.searches += 1
        timeout = self.timeout

        def run():
            # Looked up on the module at call time so tests can replace it.
            return serp_google.search(query, max_results=RESULTS_PER_QUERY,
                                      timeout=timeout, country="")

        self._pending.append((query, purpose, self._executor.submit(run), ""))

    def collect(self) -> GoogleResults:
        from apps.citations.services import serp_google

        results = GoogleResults(configured=bool(self.provider), provider=self.provider,
                                searches=self.searches, brand=self.brand)
        futures = [f for _, _, f, _ in self._pending if f is not None]
        if futures:
            # The requests carry their own timeout; this only guards a hung
            # thread so the scan never waits past it.
            concurrent.futures.wait(futures, timeout=self.timeout + 5)
        for query, purpose, future, error in self._pending:
            result = QueryResult(query=query, purpose=purpose, error=error)
            if future is not None:
                if not future.done():
                    future.cancel()
                    result.error = "timeout"
                else:
                    try:
                        payload = future.result()
                    except Exception as exc:  # serp_google never raises; be safe
                        logger.warning("onboarding: Google search raised: %s", exc)
                        payload = {"error": "network"}
                    payload = payload if isinstance(payload, dict) else {"error": "decode"}
                    result.payload = payload
                    result.error = str(payload.get("error") or "")
            if purpose == PURPOSE_COMPETITORS and result.usable:
                result.trusted = _brand_guard(result, self.brand)
            results.queries.append(result)
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
        if self.searches:
            # Onboarding saves no row to carry the cost, so it is logged with
            # the same per-search price every other Google feature records.
            cost = serp_google.cost_per_search_usd(self.provider) * self.searches
            logger.info(
                "onboarding Google usage: user=%s provider=%s searches=%d cost_usd=%s",
                self.user_key, self.provider, self.searches, cost,
            )
        return results


# -- competitors ---------------------------------------------------------------


def _is_candidate(host: str, apex: str, own_apex: str) -> bool:
    """A result site that could be a competitor's own site."""
    from apps.citations.models import SourceClass
    from apps.citations.services.domain_classifier import _rule_classify, is_aggregator

    if not apex or (own_apex and apex == own_apex):
        return False
    if is_aggregator(host) or is_aggregator(apex):
        return False
    return _rule_classify(apex, host) in (SourceClass.OTHER, SourceClass.DOCS)


def _rows(google: GoogleResults, own_apex: str) -> list[dict]:
    """Organic rows of the trusted brand searches, numbered for the prompt."""
    rows: list[dict] = []
    for q in google.competitor_queries():
        for item in q.organic:
            host = (item.get("domain") or "").lower()
            apex = apex_of(host)
            rows.append({
                "n": len(rows) + 1,
                "query": q.query,
                "rank": item.get("position") or item.get("rank"),
                "url": item.get("url") or "",
                "host": host,
                "apex": apex,
                "title": clean_text(item.get("title"), 200),
                "snippet": clean_text(item.get("snippet"), 300),
                "candidate": _is_candidate(host, apex, own_apex),
            })
    return rows


def _evidence(row: dict) -> dict:
    return {"query": row["query"], "rank": row["rank"], "url": row["url"]}


def _company_name(title: str, apex: str, brand: str) -> str:
    """The company a result belongs to: the title segment that matches the
    site's name, else a short first segment, else the site's name."""
    label = _label(apex)
    segments = [s.strip() for s in re.split(r"\s+[|–—-]\s+|:\s+", title or "") if s.strip()]
    for segment in segments:
        if _name_key(segment) == label:
            return segment[:120]
    if segments:
        first = segments[0]
        if (len(first.split()) <= 3 and len(first) >= 2
                and not (brand and whole_words(brand, first))
                and not re.search(r"\d", first)):
            return first[:120]
    return label.capitalize() if label else ""


def _fallback_pick(rows: list[dict], *, brand: str) -> list[dict]:
    """Result sites ranked by how many brand searches listed them, then by
    their best position."""
    stats: dict[str, dict] = {}
    for row in rows:
        if not row["candidate"]:
            continue
        entry = stats.setdefault(row["apex"], {"queries": set(), "best": None, "row": row})
        entry["queries"].add(row["query"])
        rank = row["rank"] if isinstance(row["rank"], int) else 999
        if entry["best"] is None or rank < entry["best"]:
            entry["best"], entry["row"] = rank, row
    ordered = sorted(stats.items(), key=lambda kv: (-len(kv[1]["queries"]), kv[1]["best"]))
    out = []
    for apex, entry in ordered:
        name = _company_name(entry["row"]["title"], apex, brand)
        if not name:
            continue
        out.append({"name": name, "domain": apex, "source": SOURCE_SEARCH,
                    "evidence": _evidence(entry["row"])})
        if len(out) >= COMPETITORS_MAX:
            break
    return out


def _claude_prompt(rows: list[dict], related: list[dict], brand: str) -> tuple[str, str]:
    from apps.llm_ranking.services.ranking_service import build_enriched_system_prompt

    lines = [f"[{r['n']}] {r['host']} | {r['title']} | {r['snippet']}" for r in rows]
    if related:
        lines.append("Related searches: " + "; ".join(r["text"] for r in related))
    system = build_enriched_system_prompt(
        "You pick a company's direct competitors from Google results. You only "
        "name companies that the numbered results themselves show; you never add "
        "companies from memory.",
        "\n".join(lines),
    )
    prompt = (
        f"The Google results in the system prompt are for searches about "
        f"{brand}. Which companies in those results directly compete with "
        f"{brand}?\n"
        "Rules:\n"
        "- Only companies named in a result's title or snippet, or whose own "
        "site the result is.\n"
        "- Not review sites, directories, publishers, marketplaces or "
        f"{brand} itself.\n"
        "- 'row' is the number of the result that shows the company.\n"
        "- 'domain' is the company's own site when a result is on it, else \"\".\n"
        "- At most 8. An empty list is better than a guess.\n\n"
        "Return ONLY a JSON array, no prose, no code fences:\n"
        '[{"name": "", "domain": "", "row": 1}]'
    )
    return prompt, system


def _validated_picks(data, rows: list[dict], brand: str, own_apex: str) -> list[dict]:
    by_n = {r["n"]: r for r in rows}
    candidates = {r["apex"] for r in rows if r["candidate"]}
    by_label: dict[str, list[str]] = {}
    for apex in sorted(candidates):
        by_label.setdefault(_label(apex), []).append(apex)
    brand_key = _name_key(brand)
    seen: set[str] = set()
    out: list[dict] = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        name = clean_text(item.get("name"), 120).strip("\"'.,;:")
        row = by_n.get(item.get("row")) if isinstance(item.get("row"), int) else None
        if row is None or not 2 <= len(name) <= 120:
            continue
        key = _name_key(name)
        if not key or key == brand_key:
            continue
        shown = whole_words(name, f"{row['title']} {row['snippet']}") or key == _label(row["apex"])
        if not shown:
            continue  # the cited result does not show this company
        domain = apex_of(str(item.get("domain") or ""))
        if domain not in candidates:
            domain = ""
        if not domain:
            matches = by_label.get(key) or []
            domain = matches[0] if len(matches) == 1 else ""
        if own_apex and domain == own_apex:
            continue
        dedupe = domain or key
        if dedupe in seen or key in seen:
            continue
        seen.update({dedupe, key})
        out.append({"name": name, "domain": domain, "source": SOURCE_BOTH,
                    "evidence": _evidence(row)})
        if len(out) >= COMPETITORS_MAX:
            break
    return out


def _claude_pick(rows: list[dict], related: list[dict], *, brand: str, own_apex: str,
                 user=None) -> list[dict]:
    from core.llm import ClaudeUtility

    provider = ClaudeUtility(model="claude-haiku-4-5", max_tokens=700)
    if not provider.is_configured() or not rows:
        return []
    try:
        prompt, system = _claude_prompt(rows[:PROMPT_ROWS_MAX], related, brand)
        result = provider.query(prompt, system, module="onboarding",
                                role="competitor_discovery_serp", user=user)
        if not result.succeeded:
            logger.debug("onboarding: Claude competitor pick skipped: %s", result.error)
            return []
        match = re.search(r"\[.*\]", result.text or "", re.DOTALL)
        if not match:
            return []
        return _validated_picks(json.loads(match.group()), rows[:PROMPT_ROWS_MAX],
                                brand, own_apex)
    except Exception as exc:
        logger.debug("onboarding: Claude competitor pick skipped: %s", exc)
        return []


def competitors_from_results(google: GoogleResults, *, business_name: str, domain: str,
                             user=None) -> list[dict]:
    """Competitors found in Google's results, ``[{name, domain, source,
    evidence: {query, rank, url}}]``; [] when the results name none."""
    if not google.has_competitor_results():
        return []
    brand = clean_text(business_name, BRAND_MAX)
    own_apex = apex_of(domain)
    rows = _rows(google, own_apex)
    picks = _claude_pick(rows, google.related(), brand=brand, own_apex=own_apex, user=user)
    return picks or _fallback_pick(rows, brand=brand)
