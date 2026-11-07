"""Products (contract 9): tracked products in Google Shopping and in the AI
engines' answers to shopping questions.

A product has match terms (a Google Shopping title or an answer that
contains every term, case and spacing ignored, is the product), the
shopping questions buyers ask, and the store names that count as sold by
the project. A ``shopping_check`` run reads one Google Shopping page per
question through ScraperAPI and asks the chosen engines each question as
written, once, without web context of its own. Prices are the strings
Google showed, never converted or summed. Engine answers stay on
``ShoppingResult``; they are never written to ``LLMRankingResult``, so the
visibility numbers do not change.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import transaction
from django.utils import timezone

from apps.citations.services import serp_google
from apps.insights.models import ShoppingResult, TrackedProduct
from apps.insights.services import google, runs
from apps.insights.services.runs import ITEM_MESSAGES, iso
from apps.insights.services.text import collapse, fold
from apps.llm_ranking.providers.claude import DEFAULT_SYSTEM
from apps.search_gap.services import country_checks, country_geo
from core.exceptions import CanseeException

logger = logging.getLogger("apps")

KEY = "shopping_check"
GOOGLE = ShoppingResult.GOOGLE_SHOPPING
QUEUED, RUNNING = ShoppingResult.STATUS_QUEUED, ShoppingResult.STATUS_RUNNING
DONE, FAILED = ShoppingResult.STATUS_DONE, ShoppingResult.STATUS_FAILED
SKIPPED = ShoppingResult.STATUS_SKIPPED
ENGINE_KEYS = country_geo.ENGINE_KEYS
ENGINE_LABELS = country_geo.ENGINE_LABELS

MAX_QUERIES = 3
MAX_TERMS = 5
MAX_SELLERS = 5
MAX_MATCHES = 10
MAX_TOP = 10
MAX_ANSWER_CHARS = 8000
EXCERPT_CHARS = 200
CONCURRENCY = 4
RETRY_SECONDS = 3.0

NAME_MESSAGE = "Name the product in 1 to 120 characters."
TERMS_MESSAGE = "Add 1 to 5 match words of 1 to 40 characters each."
URL_MESSAGE = "Enter the product page as an http or https address, or leave it empty."
QUERIES_MESSAGE = "Add 1 to 3 shopping questions of 3 to 200 characters each."
SELLERS_MESSAGE = "Add up to 5 store names of 1 to 80 characters each."
SHOPPING_NOT_CONFIGURED = "Google Shopping results need ScraperAPI on this server."
WEB_SEARCH_OFF = (
    "The engines answer without web search on this server, so their shopping answers "
    "come from training data."
)
NOTHING_TO_RUN = "Choose at least one product and Google Shopping or an engine."

# Engine failure codes kept as they are (contract 1e item codes, plus a
# skipped engine's not_configured); timeouts, cut-off and empty answers
# read engine_error.
_ENGINE_CODES = ("refused", "rate_limited", "allowance_exceeded", "engine_error", "not_configured")
# A numbered list item starts a line: "1. ", "2) ".
_NUMBERED = re.compile(r"^\s*(\d{1,2})[.)]\s")
_url = URLValidator(schemes=["http", "https"])


def _invalid(field_name: str, message: str) -> CanseeException:
    return CanseeException(message, code="invalid", status_code=400,
                           details={"field": field_name, "message": message})


# -- matching ------------------------------------------------------------------


def match(text, terms) -> bool:
    """Every term is in the text, case and spacing ignored (contract 1d)."""
    haystack = fold(text)
    keys = [fold(term) for term in terms or [] if fold(term)]
    return bool(keys) and all(key in haystack for key in keys)


def list_rank(answer, terms) -> int | None:
    """The number of the first numbered list item that names the product
    (an item runs until the next one starts), else None."""
    items: list[list] = []
    for line in (answer or "").splitlines():
        found = _NUMBERED.match(line)
        if found:
            items.append([int(found.group(1)), line])
        elif items:
            items[-1][1] += "\n" + line
    return next((number for number, text in items if match(text, terms)), None)


def excerpt(answer, terms) -> str:
    """About EXCERPT_CHARS of the answer around the first match term."""
    text = answer or ""
    starts = []
    for term in terms or []:
        words = fold(term).split()
        if words:
            found = re.search(r"\s+".join(re.escape(w) for w in words), text, re.IGNORECASE)
            if found:
                starts.append((found.start(), found.end()))
    if not starts:
        return ""
    start, end = min(starts)
    lead = max(0, (EXCERPT_CHARS - (end - start)) // 2)
    begin = max(0, start - lead)
    return collapse(text[begin:begin + EXCERPT_CHARS])


# -- products --------------------------------------------------------------------


def _texts(value, field_name: str, message: str, *, items: tuple[int, int],
           chars: tuple[int, int], folded: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise _invalid(field_name, message)
    out, seen = [], set()
    for item in value:
        if not isinstance(item, str):
            raise _invalid(field_name, message)
        text = fold(item) if folded else collapse(item)
        if not text:
            continue
        if not chars[0] <= len(text) <= chars[1]:
            raise _invalid(field_name, message)
        if fold(text) not in seen:
            seen.add(fold(text))
            out.append(text)
    if not items[0] <= len(out) <= items[1]:
        raise _invalid(field_name, message)
    return out


def _clean(website, data, product: TrackedProduct | None = None) -> dict:
    """The validated fields of a POST (all of them) or a PATCH (those sent)."""
    if not isinstance(data, dict):
        raise _invalid("body", "Send the product as a JSON object.")
    creating = product is None
    out: dict = {}
    if creating or "name" in data:
        name = collapse(data.get("name")) if isinstance(data.get("name"), str) else ""
        if not 1 <= len(name) <= 120:
            raise _invalid("name", NAME_MESSAGE)
        out["name"] = name
    if creating or "match_terms" in data:
        terms = data.get("match_terms")
        if creating and not terms:
            terms = [out["name"]]
        out["match_terms"] = _texts(terms, "match_terms", TERMS_MESSAGE,
                                    items=(1, MAX_TERMS), chars=(1, 40), folded=True)
    if creating or "url" in data:
        url = data.get("url") if data.get("url") is not None else ""
        if not isinstance(url, str):
            raise _invalid("url", URL_MESSAGE)
        url = url.strip()
        if url:
            try:
                _url(url)
            except ValidationError:
                raise _invalid("url", URL_MESSAGE) from None
            if len(url) > 2000:
                raise _invalid("url", URL_MESSAGE)
        out["url"] = url
    if creating or "queries" in data:
        out["queries"] = _texts(data.get("queries"), "queries", QUERIES_MESSAGE,
                                items=(1, MAX_QUERIES), chars=(3, 200))
    if creating or "own_sellers" in data:
        sellers = data.get("own_sellers")
        if creating and sellers is None:
            sellers = [collapse(website.name)[:80]] if collapse(website.name) else []
        out["own_sellers"] = _texts(sellers, "own_sellers", SELLERS_MESSAGE,
                                    items=(0, MAX_SELLERS), chars=(1, 80))
    return out


def create_product(website, user, data) -> TrackedProduct:
    fields = _clean(website, data)
    limit = settings.SHOPPING_MAX_PRODUCTS
    with transaction.atomic():
        from apps.websites.models import Website

        # The website row serializes creates, so two at once cannot pass the limit.
        list(Website.all_objects.select_for_update().filter(id=website.id).values_list("id"))
        if TrackedProduct.objects.filter(website=website, is_archived=False).count() >= limit:
            raise _invalid("products", f"You can track up to {limit} products.")
        return TrackedProduct.objects.create(
            website=website,
            created_by=user if getattr(user, "is_authenticated", False) else None,
            **fields,
        )


def update_product(product: TrackedProduct, data) -> TrackedProduct:
    fields = _clean(product.website, data, product)
    for name, value in fields.items():
        setattr(product, name, value)
    if fields:
        product.save(update_fields=[*fields, "updated_at"])
    return product


def archive_product(product: TrackedProduct) -> None:
    """Stop tracking a product; its results stay."""
    if not product.is_archived:
        product.is_archived = True
        product.save(update_fields=["is_archived", "updated_at"])


# -- presenters --------------------------------------------------------------------


def _latest(products, statuses=(DONE,)) -> dict:
    """{product id: {(query, engine): newest result}} with these statuses.
    Only that newest row is read for each (product, question, engine): the
    results of every check stay in the table, each with its answer text."""
    out: dict = {product.id: {} for product in products}
    rows = (
        ShoppingResult.objects
        .filter(product__in=products, status__in=statuses, checked_at__isnull=False)
        .order_by("product_id", "query", "engine", "-checked_at")
        .distinct("product_id", "query", "engine")
    )
    for row in rows:
        out[row.product_id].setdefault((row.query, row.engine), row)
    return out


def _sellers(rows) -> list[str]:
    seen, out = set(), []
    for row in rows:
        for seller in row.sellers or []:
            if seller and fold(seller) not in seen:
                seen.add(fold(seller))
                out.append(seller)
    return out


def product_payload(product: TrackedProduct, latest: dict | None = None) -> dict:
    """``Product`` (contract 9); ``latest`` from the newest done result per
    (question, engine) of the product's current questions."""
    if latest is None:
        latest = _latest([product])[product.id]
    current = [row for (query, _e), row in latest.items() if query in product.queries]
    google_rows = [row for row in current if row.engine == GOOGLE]
    engine_rows = [row for row in current if row.engine != GOOGLE]
    google_part = None
    if google_rows:
        found = [row for row in google_rows if row.state == "found"]
        positions = [row.position for row in found if row.position]
        google_part = {
            "state": "found" if found else "not_found",
            "queries_checked": len(google_rows),
            "queries_found": len(found),
            "best_position": min(positions) if positions else None,
            "sold_by_you": any(row.sold_by_you for row in found) if found else None,
            "sellers": len(_sellers(found)),
        }
    engines_part = None
    if engine_rows:
        by_engine = {}
        for key in ENGINE_KEYS:
            rows = [row for row in engine_rows if row.engine == key]
            if rows:
                named = [row for row in rows if row.state == "named"]
                ranks = [row.rank for row in named if row.rank]
                by_engine[key] = {"answers": len(rows), "named": len(named),
                                  "rank": min(ranks) if ranks else None}
        engines_part = {
            "answers": len(engine_rows),
            "named": sum(1 for row in engine_rows if row.state == "named"),
            "by_engine": by_engine,
        }
    checked = [row.checked_at for row in current if row.checked_at]
    return {
        "id": str(product.id),
        "name": product.name,
        "match_terms": product.match_terms,
        "url": product.url,
        "queries": product.queries,
        "own_sellers": product.own_sellers,
        "created_at": iso(product.created_at),
        "latest": (
            {"checked_at": iso(max(checked)), "google": google_part, "engines": engines_part}
            if google_part or engines_part else None
        ),
    }


def _engines() -> list[dict]:
    return [{"key": key, "label": ENGINE_LABELS[key], "configured": country_geo.engine_configured(key)}
            for key in ENGINE_KEYS]


def _shopping_configured() -> bool:
    return serp_google.provider() == serp_google.PROVIDER_SCRAPERAPI


def products_payload(website) -> dict:
    """``ProductsPayload`` (contract 9)."""
    products = list(
        TrackedProduct.objects.filter(website=website, is_archived=False).order_by("created_at")
    )
    latest = _latest(products)
    configured = _shopping_configured()
    return {
        "limits": {"max_products": settings.SHOPPING_MAX_PRODUCTS, "max_queries": MAX_QUERIES,
                   "max_terms": MAX_TERMS},
        "shopping": {
            "configured": configured,
            "provider_label": serp_google.PROVIDER_LABELS[serp_google.PROVIDER_SCRAPERAPI]
            if configured else None,
            "credits_per_page": runs.credits_per_page(KEY) if configured else None,
        },
        "engines": _engines(),
        "products": [product_payload(p, latest[p.id]) for p in products],
        **runs.run_state(website, KEY),
    }


def _error(row) -> dict | None:
    return {"code": row.error_code, "message": row.error_message} if row.error_code else None


def _google_detail(row, configured: bool) -> dict:
    if row is None:
        return {"state": "not_checked" if configured else "not_configured", "position": None,
                "results": None, "checked_at": None, "error": None, "matches": [],
                "lowest": None, "top": []}
    done = row.status == DONE
    return {
        "state": row.state or ("failed" if row.status == FAILED else "not_checked"),
        "position": row.position if done else None,
        "results": row.results_count if done else None,
        "checked_at": iso(row.checked_at),
        "error": None if done else _error(row),
        "matches": row.matches if done else [],
        "lowest": (row.lowest or None) if done else None,
        "top": row.top if done else [],
    }


def _engine_detail(row, key: str) -> dict:
    base = {"key": key, "label": ENGINE_LABELS.get(key, key), "state": "not_checked",
            "rank": None, "excerpt": None, "answer_text": None, "checked_at": None,
            "error": None}
    if row is None:
        return base
    if row.status != DONE:
        return {**base, "state": "failed" if row.status == FAILED else "not_checked",
                "checked_at": iso(row.checked_at), "error": _error(row)}
    return {**base, "state": row.state, "rank": row.rank, "excerpt": row.excerpt or None,
            "answer_text": row.answer_text, "checked_at": iso(row.checked_at)}


def _history(product) -> list[dict]:
    runs_seen: dict = {}
    rows = (
        ShoppingResult.objects.filter(product=product, status=DONE, checked_at__isnull=False)
        .values("run_id", "engine", "state", "position", "checked_at")
    )
    for row in rows:
        entry = runs_seen.setdefault(row["run_id"], {
            "checked_at": row["checked_at"], "google_best_position": None,
            "engines_named": 0, "engines_answers": 0,
        })
        entry["checked_at"] = max(entry["checked_at"], row["checked_at"])
        if row["engine"] == GOOGLE:
            if row["state"] == "found" and row["position"]:
                best = entry["google_best_position"]
                entry["google_best_position"] = row["position"] if best is None else min(best, row["position"])
        else:
            entry["engines_answers"] += 1
            entry["engines_named"] += int(row["state"] == "named")
    ordered = sorted(runs_seen.values(), key=lambda e: e["checked_at"], reverse=True)
    return [{**entry, "checked_at": iso(entry["checked_at"])} for entry in ordered]


def product_detail(product: TrackedProduct) -> dict:
    """``ProductDetail`` (contract 9): the product, then per question the
    newest Google Shopping read and each engine's newest answer, and the
    history of its checks."""
    finished = _latest([product], statuses=(DONE, FAILED, SKIPPED))[product.id]
    payload = product_payload(product, {k: r for k, r in finished.items() if r.status == DONE})
    asked = {engine for (_q, engine) in finished if engine != GOOGLE}
    engines = [key for key in ENGINE_KEYS if key in asked]
    configured = _shopping_configured()
    payload["queries"] = [
        {
            "query": query,
            "google": _google_detail(finished.get((query, GOOGLE)), configured),
            "engines": [_engine_detail(finished.get((query, key)), key) for key in engines],
        }
        for query in product.queries
    ]
    payload["history"] = _history(product)
    return payload


# -- kind shopping_check -----------------------------------------------------------


@dataclass
class ShoppingPlan:
    products: list = field(default_factory=list)
    engines: list = field(default_factory=list)
    google: bool = False

    @property
    def questions(self) -> int:
        return sum(len(product.queries) for product in self.products)

    @property
    def pages(self) -> int:
        return self.questions if self.google else 0

    @property
    def answers(self) -> int:
        return self.questions * len(self.engines)


def validate(website, params) -> ShoppingPlan:
    ids = params.get("product_ids") or []
    engines = params.get("engines") or []
    google_on = params.get("google_shopping", False)
    if not isinstance(google_on, bool):
        raise _invalid("google_shopping", "Choose whether to check Google Shopping.")
    if not isinstance(ids, list):
        raise _invalid("product_ids", "Choose products this project tracks.")
    if not isinstance(engines, list) or any(e not in ENGINE_KEYS for e in engines):
        raise _invalid("engines", "Choose engines from chatgpt, claude, gemini, grok or perplexity.")
    engines = [key for key in ENGINE_KEYS if key in engines]
    for key in engines:
        if not country_geo.engine_configured(key):
            raise _invalid("engines", f"{ENGINE_LABELS[key]} isn't configured on this server.")
    wanted = []
    for value in ids:
        try:
            wanted.append(uuid.UUID(str(value)))
        except ValueError:
            raise _invalid("product_ids", "Choose products this project tracks.") from None
    by_id = {
        p.id: p for p in TrackedProduct.objects.filter(website=website, is_archived=False,
                                                        id__in=wanted)
    }
    if any(pid not in by_id for pid in wanted):
        raise _invalid("product_ids", "Choose products this project tracks.")
    products = list({pid: by_id[pid] for pid in wanted}.values())
    if not products or not (engines or google_on):
        raise CanseeException(NOTHING_TO_RUN, code="nothing_to_run", status_code=400)
    limit = settings.SHOPPING_MAX_PRODUCTS_PER_RUN
    if len(products) > limit:
        raise CanseeException(f"This run is over the limit of {limit} products.",
                              code="too_many", status_code=400)
    if google_on and not _shopping_configured():
        raise CanseeException(SHOPPING_NOT_CONFIGURED, code="not_configured", status_code=400)
    plan = ShoppingPlan(products, engines, google_on)
    answers_limit = settings.SHOPPING_MAX_ENGINE_ANSWERS
    if plan.answers > answers_limit:
        raise CanseeException(f"This run is over the limit of {answers_limit} answers.",
                              code="too_many", status_code=400)
    return plan


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def estimate(website, user, plan: ShoppingPlan) -> dict:
    usd = sum((country_checks.answer_usd_max(e) for e in plan.engines), Decimal("0"))
    parts = []
    if plan.pages:
        parts.append(f"{_plural(plan.pages, 'Google Shopping page')}")
    if plan.answers:
        parts.append(f"{_plural(plan.answers, 'engine answer')}")
    warnings = []
    if plan.engines and not getattr(settings, "LLM_WEBSEARCH_ENABLED", False):
        warnings.append({"code": "web_search_off", "message": WEB_SEARCH_OFF})
    return {
        "summary": f"{_plural(len(plan.products), 'product')}: {' and '.join(parts)}",
        "units": {"products": len(plan.products), "google_pages": plan.pages,
                  "answers": plan.answers},
        "ai": {"calls_max": plan.answers, "usd_max": usd * plan.questions},
        "google": {"pages_max": plan.pages},
        "fetches": {"pages_max": 0},
        "warnings": warnings,
    }


def choices(website, user) -> dict:
    products = TrackedProduct.objects.filter(website=website, is_archived=False).order_by("created_at")
    return {
        "products": [{"id": str(p.id), "name": p.name, "queries": len(p.queries)} for p in products],
        "engines": _engines(),
        "google_shopping": {"configured": _shopping_configured()},
    }


def plan_items(run, plan: ShoppingPlan) -> int:
    rows = []
    for product in plan.products:
        for query in product.queries:
            engines = ([GOOGLE] if plan.google else []) + plan.engines
            rows += [
                ShoppingResult(run=run, website=run.website, product=product, query=query[:200],
                               engine=engine, status=QUEUED)
                for engine in engines
            ]
    ShoppingResult.objects.bulk_create(rows)
    return len(rows)


def _failure(status: str, code: str, message: str, state: str) -> dict:
    return {"status": status, "state": state, "error_code": code, "error_message": message[:300]}


def _shopping(query: str) -> dict:
    try:
        return serp_google.shopping_search(query)
    except Exception:
        logger.warning("insights: shopping_search raised", exc_info=True)
        return {"error": "network"}


def google_fields(results: list, product) -> dict:
    """A done Google Shopping row's fields from the page's results."""
    own = {fold(seller) for seller in product.own_sellers or []}
    rows = [{**r, "is_match": match(r.get("title"), product.match_terms)} for r in results]
    matches = [r for r in rows if r["is_match"]]
    priced = [r for r in rows if r.get("price_value") is not None]
    lowest = min(priced, key=lambda r: r["price_value"]) if priced else None
    sellers, seen = [], set()
    for r in matches:
        if r.get("seller") and fold(r["seller"]) not in seen:
            seen.add(fold(r["seller"]))
            sellers.append(r["seller"])
    return {
        "status": DONE,
        "state": "found" if matches else "not_found",
        "position": matches[0]["position"] if matches else None,
        "results_count": len(rows),
        "matches": [
            {"position": r["position"], "title": r["title"], "seller": r.get("seller", ""),
             "price": r.get("price", ""), "sold_by_you": fold(r.get("seller")) in own}
            for r in matches[:MAX_MATCHES]
        ],
        "top": [
            {"position": r["position"], "title": r["title"], "seller": r.get("seller", ""),
             "price": r.get("price", ""), "is_match": r["is_match"]}
            for r in rows[:MAX_TOP]
        ],
        "lowest": (
            {"title": lowest["title"], "seller": lowest.get("seller", ""),
             "price": lowest.get("price", "")} if lowest else {}
        ),
        "sellers": sellers,
        "sold_by_you": any(fold(r.get("seller")) in own for r in matches) if matches else None,
        "error_code": "",
        "error_message": "",
    }


def _google_row(ctx, row) -> dict:
    if not ctx.google_page():
        return _failure(SKIPPED, "daily_limit", ITEM_MESSAGES["daily_limit"], "not_checked")
    payload = _shopping(row.query)
    if google.billed(payload):
        ctx.billed_page()
    else:
        error = str(payload.get("error") or "")
        retry = error in ("network", "http_429") or error.startswith("http_5")
        if retry:
            # Not billed, so one more request stays inside the confirmed pages.
            time.sleep(RETRY_SECONDS)
            if not ctx.cancelled() and ctx.google_page():
                payload = _shopping(row.query)
                if google.billed(payload):
                    ctx.billed_page()
    error = str(payload.get("error") or "")
    if error == "not_configured":
        return _failure(SKIPPED, "not_configured", SHOPPING_NOT_CONFIGURED, "not_configured")
    if error:
        code, message = google.error_message(error)
        return _failure(FAILED, code, message, "failed")
    return google_fields(payload.get("results") or [], row.product)


def _engine_row(ctx, row) -> dict:
    cls = country_geo.provider_class(row.engine)
    instance = cls() if cls is not None else None
    if instance is None or not instance.is_configured():
        return _failure(SKIPPED, "not_configured", country_checks.CELL_MESSAGES["not_configured"],
                        "not_checked")
    model_id = str(getattr(instance, "model", "") or "")[:64]
    result = country_checks.query_with_retry(
        instance, row.query, system_prompt=DEFAULT_SYSTEM, user=ctx.user, website=ctx.website,
        audit_id=ctx.audit_id, role="shopping_answer", module="insights",
        extra_metadata={"actor": "user", "feature": "shopping"},
    )
    text = (result.text or "").strip() if result is not None and result.succeeded else ""
    if not text:
        if result is None:
            status, code = FAILED, "engine_error"
        else:
            status, code = country_checks.engine_error(
                result.error if not result.succeeded else "empty_answer:",
            )
        state = "failed" if status == FAILED else "not_checked"
        # The code is one of contract 1e's item codes; the words stay the
        # engine's own reason (wave 1 2h), as Google failures keep theirs.
        public = code if code in _ENGINE_CODES else "engine_error"
        return {**_failure(status, public, country_checks.CELL_MESSAGES[code], state),
                "model_id": model_id}
    terms = row.product.match_terms
    named = match(text, terms)
    return {
        "status": DONE,
        "state": "named" if named else "not_named",
        "rank": list_rank(text, terms) if named else None,
        "excerpt": excerpt(text, terms)[:400] if named else "",
        "answer_text": text[:MAX_ANSWER_CHARS],
        "model_id": model_id,
        "error_code": "",
        "error_message": "",
    }


def _work(ctx, row_id) -> None:
    if ctx.cancelled():
        # Left queued: finalize ends it as stopped.
        return
    if not ShoppingResult.objects.filter(id=row_id, status=QUEUED).update(status=RUNNING):
        return
    ctx.start_item()
    row = ShoppingResult.objects.select_related("product").get(id=row_id)
    try:
        fields = _google_row(ctx, row) if row.engine == GOOGLE else _engine_row(ctx, row)
    except Exception:
        logger.exception("insights: shopping result %s failed", row_id)
        fields = _failure(FAILED, "internal_error", ITEM_MESSAGES["internal_error"], "failed")
    fields["checked_at"] = timezone.now()
    # Only over our own "running": a row the stale sweep already ended keeps that end.
    ShoppingResult.objects.filter(id=row_id, status=RUNNING).update(**fields)
    ctx.end_item(fields["status"], error=fields.get("error_code") or None)


def execute(ctx) -> None:
    ids = list(
        ShoppingResult.objects.filter(run=ctx.run, status=QUEUED)
        .order_by("product_id", "query", "engine").values_list("id", flat=True)
    )
    with ctx.pool(CONCURRENCY) as pool:
        for row_id in ids:
            pool.submit(_work, ctx, row_id)


KIND = runs.Kind(
    key=KEY, label="Shopping check", queue="ai", validate=validate, estimate=estimate,
    choices=choices, plan_items=plan_items, execute=execute,
)
runs.register(KIND)
