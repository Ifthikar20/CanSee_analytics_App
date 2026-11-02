"""Rule-based "what to change, and where" for one tracked prompt.

Pure: takes the gap picture as plain data and returns ranked actions,
each naming the pages to work on. The rules follow the gap classes in
``scoring``:

* AI ahead of Google: search work. Make your page rank for the prompt,
  and earn links from the sites both Google and the AI answers trust.
* Google ahead of AI: answer-engine work. Get onto the pages the engines
  cite, and make the page Google ranks easy for an engine to quote.
* A competitor above you on both: study the page that wins.
* Invisible in both: publish a page for the prompt first.
* Strong in both: hold.

Priority is the summed gap of the engines an action addresses, so the
biggest, most widespread gaps come first.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from apps.citations.services.url_normalizer import extract_apex_domain
from apps.search_gap.services.scoring import (
    AI_AHEAD,
    GOOGLE_AHEAD,
    INVISIBLE_BOTH,
    NO_DATA,
    STRONG_BOTH,
    google_page,
    search_score,
)

MAX_TARGETS = 5
LIVE_WEB_ENGINES = {"perplexity"}


def _join(items: list[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _names(engines: list[dict]) -> str:
    return _join([e["label"] for e in engines])


def _plural(engines: list[dict], one: str, many: str) -> str:
    return one if len(engines) == 1 else many


def _short(url: str) -> str:
    parts = urlsplit(url or "")
    host = parts.netloc[4:] if parts.netloc.startswith("www.") else parts.netloc
    path = parts.path.rstrip("/")
    label = f"{host}{path}" or url
    return label if len(label) <= 60 else f"{label[:57]}..."


def _gap_weight(engines: list[dict], search: int) -> int:
    return sum(abs((e["score"] or 0) - search) for e in engines)


def _action(kind, title, detail, urls, engines, priority) -> dict:
    seen, targets = set(), []
    for url in urls:
        if url and url not in seen:
            seen.add(url)
            targets.append({"url": url, "label": _short(url)})
    return {
        "kind": kind,
        "title": title,
        "detail": detail,
        "targets": targets[:MAX_TARGETS],
        "engines": [e["key"] for e in engines],
        "priority": priority,
    }


def _own_cited(engines: list[dict]) -> list[str]:
    urls = []
    for e in engines:
        for c in e.get("citations") or []:
            if c.get("is_yours") and c.get("url") not in urls:
                urls.append(c["url"])
    return urls


def _third_party_cited(engines: list[dict]) -> list[str]:
    """Pages the engines cite that are not yours, most-cited first."""
    counts: dict[str, int] = {}
    for e in engines:
        for c in e.get("citations") or []:
            if not c.get("is_yours") and c.get("url"):
                counts[c["url"]] = counts.get(c["url"], 0) + 1
    return sorted(counts, key=lambda u: -counts[u])


def _best_rank_phrase(engines: list[dict]) -> str:
    ranks = [e["rank"] for e in engines if e.get("mentioned") and e.get("rank")]
    if ranks:
        verb = _plural(engines, "ranks", "rank")
        return f"already {verb} you as high as #{min(ranks)}"
    return f"already {_plural(engines, 'mentions', 'mention')} you"


def _brand_above(engine: dict, name: str) -> bool:
    """Does ``engine`` name ``name`` ahead of you (or name it while you are absent)?"""
    fold = name.casefold()
    for b in engine.get("brands") or []:
        if (b.get("name") or "").casefold() != fold:
            continue
        if not engine.get("mentioned"):
            return True
        mine, theirs = engine.get("rank"), b.get("position")
        return bool(mine and theirs and theirs < mine)
    return False


def build_actions(*, query: str, site_apex: str, google: dict, engines: list[dict],
                  competitors: list[dict], overall_class: str) -> list[dict]:
    """Ranked actions for one prompt.

    ``google``: {status, position, url, depth, top_results[{position, url, domain}]}.
    ``engines``: [{key, label, class, score, mentioned, rank,
                   citations[{url, is_yours}], brands[{name, position}]}].
    ``competitors``: [{name, domain}] from the project settings.
    """
    if google.get("status") != "ok":
        detail = (
            "The last Google check failed. Run it again to compare Google with the AI answers."
            if google.get("status") == "error"
            else "Run a Google check for this prompt to compare it with the AI answers."
        )
        return [_action("check_google", "Check where Google ranks you", detail, [], [], 1)]

    position = google.get("position")
    search = search_score(position)
    depth = google.get("depth") or 100
    answered = [e for e in engines if e.get("class") != NO_DATA]
    ai_ahead = [e for e in engines if e.get("class") == AI_AHEAD]
    google_ahead = [e for e in engines if e.get("class") == GOOGLE_AHEAD]
    top_results = google.get("top_results") or []
    actions: list[dict] = []

    if ai_ahead:
        weight = _gap_weight(ai_ahead, search)
        own = _own_cited(ai_ahead)
        target = google.get("url") if position else (own[0] if own else "")
        if position:
            where = f"Google has it at #{position} (page {google_page(position)})"
        else:
            where = f"Google doesn't show {site_apex} in its top {depth} results"
        if target:
            actions.append(_action(
                "make_page_rank",
                f"Make {_short(target)} rank on Google for this prompt",
                f"{_names(ai_ahead)} {_best_rank_phrase(ai_ahead)}. {where}. Use the prompt's "
                "wording in the page title, H1 and first paragraph, link to the page from your "
                "strongest pages, and confirm it is indexed in Search Console.",
                [target, *own], ai_ahead, weight,
            ))
        else:
            # No ranking page and no cited page of yours: nothing to optimise yet.
            actions.append(_action(
                "create_page",
                "Publish a page that answers this prompt",
                f"{_names(ai_ahead)} {_best_rank_phrase(ai_ahead)}, but {where} and none of "
                f"your pages is cited. Give Google a page to rank that answers \"{query}\" "
                "directly.",
                [], ai_ahead, weight,
            ))
        cited_domains = {
            extract_apex_domain(urlsplit(u).netloc)
            for u in _third_party_cited(answered)
        }
        shared = [
            r for r in top_results
            if extract_apex_domain(r.get("domain", "")) in cited_domains
            and extract_apex_domain(r.get("domain", "")) != site_apex
        ]
        if shared:
            domains = []
            for r in shared:
                if r["domain"] not in domains:
                    domains.append(r["domain"])
            actions.append(_action(
                "earn_links",
                f"Earn a mention on {_join(domains[:3])}",
                "Google's top results and the AI answers both lean on these sites for this "
                "prompt, so a listing, review or guest article there helps on both sides.",
                [r["url"] for r in shared], ai_ahead, round(weight * 0.6),
            ))

    if google_ahead:
        weight = _gap_weight(google_ahead, search)
        sources = _third_party_cited(google_ahead)
        if sources:
            detail = (
                f"{_names(google_ahead)} {_plural(google_ahead, 'builds its', 'build their')} "
                "answer from these pages, and you are not on them."
            )
            if any(e["key"] in LIVE_WEB_ENGINES for e in google_ahead):
                detail += (
                    " Perplexity reads the live web, so a new mention shows up there within days;"
                    " the other engines mostly answer from training data and move slower."
                )
            actions.append(_action(
                "get_cited",
                f"Get onto the pages {_names(google_ahead)} {_plural(google_ahead, 'cites', 'cite')}",
                detail, sources, google_ahead, weight,
            ))
        if position and google.get("url"):
            absent = not any(e.get("mentioned") for e in google_ahead)
            if absent:
                state = _plural(google_ahead, "doesn't mention you", "don't mention you")
            else:
                state = _plural(google_ahead, "ranks you low", "rank you low")
            actions.append(_action(
                "make_quotable",
                f"Make {_short(google['url'])} easy for AI to quote",
                f"It ranks #{position} on Google, but {_names(google_ahead)} {state}. Open with a "
                "direct answer to the prompt, then add a comparison table, pricing or specs, and "
                "a short FAQ. Answer engines lift self-contained facts.",
                [google["url"]], google_ahead, round(weight * 0.8),
            ))

    competitor_names = {
        extract_apex_domain(c.get("domain", "")): c.get("name", "")
        for c in competitors or [] if c.get("domain") and c.get("name")
    }
    for r in sorted(top_results, key=lambda r: r.get("position") or 999):
        apex = extract_apex_domain(r.get("domain", ""))
        if apex == site_apex or apex not in competitor_names:
            continue
        if position and (r.get("position") or 999) >= position:
            break
        name = competitor_names[apex]
        above = [e for e in answered if _brand_above(e, name)]
        if above:
            actions.append(_action(
                "study_competitor",
                f"Study {name}'s page",
                f"{name} ranks #{r['position']} on Google and {_names(above)} "
                f"{_plural(above, 'names', 'name')} them ahead of you. Compare what their page "
                "covers that yours doesn't.",
                [r["url"]], above, 20 * len(above),
            ))
        break

    if overall_class == INVISIBLE_BOTH:
        actions.append(_action(
            "create_page",
            "Publish a page that answers this prompt",
            f"Neither Google (top {depth}) nor the AI engines surface you for \"{query}\". Start "
            "with one page that answers it directly, then work on the sources the engines cite.",
            [], answered, 100 * max(1, len(answered)),
        ))
        sources = _third_party_cited(answered)
        if sources:
            actions.append(_action(
                "get_cited",
                "Get onto the pages the AI engines cite",
                "These are the pages the engines build their answers from for this prompt.",
                sources, answered, 50 * max(1, len(answered)),
            ))

    if not actions and overall_class == STRONG_BOTH:
        actions.append(_action(
            "hold",
            "Hold the position",
            "Google and the AI engines both rank you well for this prompt. Keep the ranking "
            "page current and let the schedule recheck it.",
            [google["url"]] if position else [], answered, 0,
        ))

    # One action per kind, highest priority first.
    seen, ranked = set(), []
    for a in sorted(actions, key=lambda a: -a["priority"]):
        if a["kind"] not in seen:
            seen.add(a["kind"])
            ranked.append(a)
    return ranked
