"""AI token usage rollups: the one place usage is counted for reporting.

Source of truth
---------------
``AITokenUsage`` (the accounts usage ledger) is the only table that is
read. Every model call in the product goes through
``core.ai_tracking.record_usage``, which writes exactly one ledger row per
priced unit, deduplicated by ``idempotency_key`` (a Celery retry or a
replayed agent step collapses to the original row). The other places that
carry token numbers are derived from those same rows and are never added
in:

* ``AgentRun.input_tokens`` / ``output_tokens`` / ``cache_*`` are the run's
  running sum of the ledger rows ``apps.agent.llm.usage.record_request_usage``
  writes for it (module ``agent`` and the research modules).
* ``LLMRankingAudit.total_tokens`` is ``_roll_up_cost``: a SUM over the
  ledger rows tagged with the audit id.
* ``PolarEventOutbox`` is the metering mirror of each ledger row, written in
  the same transaction.

Adding any of them to the ledger would count the same call two or three
times. Reading only the ledger also covers modules that have no rollup of
their own (onboarding, RAG embeddings, brand vault, content studio, ...).

Definitions
-----------
* ``input`` / ``output``: SUM of the ledger columns. For Anthropic calls
  ``input`` is uncached input; prompt-cache reads and writes are priced into
  the cost and kept in the row's metadata, not in these columns.
* ``total``: SUM of ``total_tokens`` (record_usage stores input + output).
* ``requests``: the number of ledger rows, i.e. priced model calls. An agent
  request whose response carries several usage iterations writes one row
  per iteration.
* ``month``: the current calendar month in UTC, from the first of the month
  at 00:00:00 UTC up to the moment of the query.
* Rows with no user (pre-signup onboarding scans) belong to no account; the
  totals report them separately as ``unattributed`` so the per-user and
  per-account numbers add up to the attributed totals exactly.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from django.db.models import Count, Q, Sum

_FIELDS = ("input", "output", "total", "requests")


def month_start_utc(now: datetime | None = None) -> datetime:
    """00:00:00 UTC on the first day of ``now``'s month (UTC)."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _aggregates(month_start: datetime) -> dict:
    in_month = Q(created_at__gte=month_start)
    return {
        "all_input": Sum("input_tokens"),
        "all_output": Sum("output_tokens"),
        "all_total": Sum("total_tokens"),
        "all_requests": Count("id"),
        "all_cost": Sum("estimated_cost_usd"),
        "month_input": Sum("input_tokens", filter=in_month),
        "month_output": Sum("output_tokens", filter=in_month),
        "month_total": Sum("total_tokens", filter=in_month),
        "month_requests": Count("id", filter=in_month),
        "month_cost": Sum("estimated_cost_usd", filter=in_month),
    }


def _cost(value) -> str:
    return str((value or Decimal("0")).quantize(Decimal("0.000001")))


def _block(row: dict | None) -> dict:
    """``{"tokens": {"month": {...}, "all_time": {...}}, "cost_usd": {...}}``."""
    row = row or {}
    return {
        "tokens": {
            period: {f: int(row.get(f"{prefix}_{f}") or 0) for f in _FIELDS}
            for period, prefix in (("month", "month"), ("all_time", "all"))
        },
        "cost_usd": {
            "month": _cost(row.get("month_cost")),
            "all_time": _cost(row.get("all_cost")),
        },
    }


def empty_usage() -> dict:
    return _block(None)


def usage_by_user(user_ids, month_start: datetime) -> dict:
    """``{user_id: usage block}`` for ``user_ids`` in ONE grouped query.
    Users without ledger rows are absent; use ``empty_usage()`` for them."""
    from apps.accounts.models import AITokenUsage

    ids = list(user_ids)
    if not ids:
        return {}
    rows = (
        AITokenUsage.objects.filter(user_id__in=ids)
        .order_by()
        .values("user_id")
        .annotate(**_aggregates(month_start))
    )
    return {row["user_id"]: _block(row) for row in rows}


def usage_totals(month_start: datetime) -> dict:
    """Ledger totals: ``attributed`` (rows with a user, which is what the
    per-user and per-account numbers add up to) and ``unattributed``."""
    from apps.accounts.models import AITokenUsage

    aggs = _aggregates(month_start)
    attributed = AITokenUsage.objects.filter(user__isnull=False).aggregate(**aggs)
    unattributed = AITokenUsage.objects.filter(user__isnull=True).aggregate(**aggs)
    return {"attributed": _block(attributed), "unattributed": _block(unattributed)}


def add_usage(blocks) -> dict:
    """Sum usage blocks (an account's seats). Exact: integers and Decimals."""
    tokens = {p: dict.fromkeys(_FIELDS, 0) for p in ("month", "all_time")}
    cost = {"month": Decimal("0"), "all_time": Decimal("0")}
    for block in blocks:
        for period in tokens:
            for f in _FIELDS:
                tokens[period][f] += block["tokens"][period][f]
            cost[period] += Decimal(block["cost_usd"][period])
    return {
        "tokens": tokens,
        "cost_usd": {p: _cost(v) for p, v in cost.items()},
    }
