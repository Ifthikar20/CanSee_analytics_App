"""Competitors (contract 4a): the project against the competitors it tracks,
in AI answers, in the sources those answers cite, on Google page one and in
crawler readiness.

A competitor counts as named when an entry of the answer's
``competitors_mentioned`` equals its name or the first label of its domain,
case and punctuation ignored (contract 1d); its rank is that entry's
position. Competitors are known on Google only from page one of each
prompt's latest check. Sections without data are null, never zeros.
"""

from __future__ import annotations

import re
from datetime import timedelta

from django.utils import timezone

from apps.citations.services import serp_google
from apps.citations.services.url_normalizer import extract_apex_domain
from apps.insights.services import answer_analysis as aa
from apps.insights.services import runs
from apps.llm_ranking.services import answer_window
from apps.search_gap.services.google_rank import is_same_site, site_apex

MAX_COMPETITORS = 5
GOOGLE_DAYS = 120
PAGE_ONE = 10
CITATION_CHUNK = 1000
_NOT_ALNUM = re.compile(r"[\W_]+")


def fold_name(value) -> str:
    """Case folded, everything but letters and digits removed."""
    return _NOT_ALNUM.sub("", str(value or "").casefold())


def _domain(value) -> str:
    host = str(value or "").strip().lower()
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0].split(":", 1)[0]
    return extract_apex_domain(host)


def brands(website) -> list[dict]:
    """You, then up to five tracked competitors as ``b1``..``b5``, each with
    ``apex`` and the folded names an answer may name it by."""
    apex = site_apex(website.url)
    out = [{"key": "you", "name": website.name, "domain": apex, "is_you": True,
            "apex": apex, "names": set()}]
    for c in (website.competitors or [])[:MAX_COMPETITORS]:
        if not isinstance(c, dict):
            continue
        name = " ".join(str(c.get("name") or "").split())
        domain = _domain(c.get("domain"))
        if not name and not domain:
            continue
        names = {fold_name(name)} if name else set()
        if domain:
            names.add(fold_name(domain.split(".", 1)[0]))
        names.discard("")
        out.append({"key": f"b{len(out)}", "name": name or domain, "domain": domain,
                    "is_you": False, "apex": domain, "names": names})
    return out


def _rank(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def mentions(result, rivals: list[dict]) -> dict:
    """``{brand key: rank or None}`` for every brand this answer names."""
    out = {}
    if result.is_mentioned:
        out["you"] = _rank(result.mention_rank)
    for item in result.competitors_mentioned or []:
        name = item.get("name") if isinstance(item, dict) else item
        folded = fold_name(name)
        if not folded:
            continue
        for brand in rivals:
            if brand["key"] not in out and folded in brand["names"]:
                out[brand["key"]] = _rank(item.get("position") if isinstance(item, dict) else None)
    return out


class _BrandTally:
    __slots__ = ("named", "ranks", "first")

    def __init__(self):
        self.named = 0
        self.ranks: list[int] = []
        self.first = 0

    def add(self, rank) -> None:
        self.named += 1
        if rank is not None:
            self.ranks.append(rank)
            if rank == 1:
                self.first += 1


def _citation_rows(result_ids: list) -> list[dict]:
    from apps.citations.models import Citation

    rows: list[dict] = []
    for offset in range(0, len(result_ids), CITATION_CHUNK):
        rows.extend(
            Citation.objects.filter(result_id__in=result_ids[offset:offset + CITATION_CHUNK])
            .values("result_id", "domain", "apex_domain", "normalized_url", "is_target")
        )
    return rows


def latest_checks(website, brand_prompts) -> dict:
    """``{brand prompt id: (latest error-free check or None, latest check)}``
    of the last GOOGLE_DAYS days."""
    from apps.search_gap.models import GoogleRankCheck

    since = timezone.now() - timedelta(days=GOOGLE_DAYS)
    out: dict = {}
    checks = (
        GoogleRankCheck.objects
        .filter(website=website, brand_prompt__in=[bp.id for bp in brand_prompts],
                checked_at__gte=since)
        .only("brand_prompt_id", "position", "depth", "top_results", "error", "checked_at")
        .order_by("-checked_at")
    )
    for check in checks:
        ok, latest = out.get(check.brand_prompt_id, (None, None))
        if latest is None:
            latest = check
        if ok is None and not check.error:
            ok = check
        out[check.brand_prompt_id] = (ok, latest)
    return out


def _google_for(check, brand: dict) -> dict:
    """``{position, depth}`` of one brand in one check (contract 4a)."""
    top = check.top_results or []
    if brand["is_you"]:
        return {"position": check.position, "depth": check.depth}
    position = None
    for row in top:
        if brand["apex"] and is_same_site(row.get("domain", ""), brand["apex"]):
            position = row.get("position")
            break
    return {"position": position, "depth": len(top)}


def _site(website, all_brands: list[dict]) -> dict:
    """``{brand key: readiness of its latest audited home page or None}``."""
    from apps.insights.models import SiteAudit

    wanted = {b["apex"]: b["key"] for b in all_brands if b["apex"]}
    out = {b["key"]: None for b in all_brands}
    for audit in (SiteAudit.objects.filter(website=website).order_by("-audited_at")
                  .prefetch_related("pages")):
        key = wanted.get(extract_apex_domain((audit.domain or "").lower()))
        if key is None or out[key] is not None:
            continue
        pages = list(audit.pages.all())
        home = next((p for p in pages if p.url == audit.home_url), pages[0] if pages else None)
        out[key] = {
            "audited_at": runs.iso(audit.audited_at),
            "checks_passed": home.passed if home else None,
            "checks_total": home.total if home else None,
            "ai_search_allowed": (home.checks or {}).get("ai_search_allowed") if home else None,
        }
    return out


def build(website, *, period="30d", interval=None, engine=None) -> dict:
    """The Competitors payload (contract 4a)."""
    period, interval, engine, _prompt = answer_window.parse_filters(
        period=period, interval=interval, engine=engine,
    )
    all_brands = brands(website)
    rivals = all_brands[1:]
    keys = [b["key"] for b in all_brands]
    brand_prompts = aa.tracked_prompts(website)
    window, current, _previous = aa.window_answers(
        website, brand_prompts, period=period, interval=interval, engine=engine,
    )
    n = len(window.buckets)

    tallies = {k: _BrandTally() for k in keys}
    bucket_answers = [0] * n
    bucket_named = {k: [0] * n for k in keys}
    pair = {b["key"]: {"both_named": 0, "you_ahead": 0, "they_ahead": 0, "no_order": 0}
            for b in rivals}
    prompt_answers: dict = {bp.id: 0 for bp in brand_prompts}
    prompt_named: dict = {bp.id: {k: 0 for k in keys} for bp in brand_prompts}
    for bp, result, index in current:
        named = mentions(result, rivals)
        bucket_answers[index] += 1
        prompt_answers[bp.id] = prompt_answers.get(bp.id, 0) + 1
        for key, rank in named.items():
            tallies[key].add(rank)
            bucket_named[key][index] += 1
            prompt_named[bp.id][key] += 1
        if "you" in named:
            for brand in rivals:
                if brand["key"] not in named:
                    continue
                row = pair[brand["key"]]
                row["both_named"] += 1
                mine, theirs = named["you"], named[brand["key"]]
                if mine is None or theirs is None or mine == theirs:
                    row["no_order"] += 1
                elif mine < theirs:
                    row["you_ahead"] += 1
                else:
                    row["they_ahead"] += 1

    answers = len(current)
    ai = {"answers": answers, "brands": {}}
    for key in keys:
        t = tallies[key]
        ai["brands"][key] = {
            "named": t.named,
            "visibility_pct": answer_window.pct(t.named, answers),
            "avg_rank": round(sum(t.ranks) / len(t.ranks), 1) if t.ranks else None,
            "first_place": t.first,
            "series": [answer_window.pct(bucket_named[key][i], bucket_answers[i]) for i in range(n)],
        }

    # Citations: the window answers' sources on each brand's site.
    rows = _citation_rows([result.id for _bp, result, _i in current])
    if rows:
        cited = {k: {"citations": 0, "pages": set(), "answers": set()} for k in keys}
        own = all_brands[0]["apex"]
        for row in rows:
            if row["is_target"] or is_same_site(row["domain"], own):
                key = "you"
            else:
                key = next((b["key"] for b in rivals
                            if b["apex"] and (row["apex_domain"] or "") == b["apex"]), None)
            if key is None:
                continue
            cited[key]["citations"] += 1
            cited[key]["pages"].add(row["normalized_url"])
            cited[key]["answers"].add(row["result_id"])
        citations = {"has_data": True, "brands": {
            k: {"citations": v["citations"], "pages": len(v["pages"]), "answers": len(v["answers"])}
            for k, v in cited.items()
        }}
    else:
        citations = {"has_data": False, "brands": None}

    # Google: the latest error-free check of each tracked prompt.
    configured = serp_google.is_configured()
    checks = latest_checks(website, brand_prompts)
    ok_checks = [ok for ok, _latest in checks.values() if ok is not None]
    google_brands = None
    if configured and ok_checks:
        google_brands = {}
        for brand in all_brands:
            positions = []
            for check in ok_checks:
                position = _google_for(check, brand)["position"]
                if position is not None:
                    positions.append(position)
            google_brands[brand["key"]] = {
                "on_page_one": sum(1 for p in positions if p <= PAGE_ONE),
                "best_position": min(positions) if positions else None,
            }
    google = {
        "configured": configured,
        "prompts_checked": len(ok_checks),
        "oldest_check_at": runs.iso(min(c.checked_at for c in ok_checks)) if ok_checks else None,
        "brands": google_brands,
    }

    by_prompt = []
    for bp in brand_prompts:
        ok, latest = checks.get(bp.id, (None, None))
        state = "ok" if ok is not None else ("failed" if latest is not None else "not_checked")
        total = prompt_answers.get(bp.id, 0)
        by_prompt.append({
            "prompt_id": str(bp.prompt_id),
            "text": bp.prompt.text,
            "answers": total,
            "ai": {k: answer_window.pct(prompt_named[bp.id][k], total) for k in keys},
            "google_state": state,
            "google_checked_at": runs.iso(ok.checked_at) if ok is not None else None,
            "google": {b["key"]: _google_for(ok, b) for b in all_brands} if ok is not None else None,
        })

    return {
        "window": aa.window_payload(window),
        "filters": {"engine": engine},
        "brands": [{k: b[k] for k in ("key", "name", "domain", "is_you")} for b in all_brands],
        "empty_reason": None if rivals else "no_competitors",
        "ai": ai,
        "head_to_head": [{"key": key, **counts} for key, counts in pair.items()],
        "citations": citations,
        "google": google,
        "by_prompt": by_prompt,
        "site": _site(website, all_brands),
    }
