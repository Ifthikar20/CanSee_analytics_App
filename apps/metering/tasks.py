"""
Celery tasks for Polar event delivery, cap notifications, and retention.

Delivery model: at-least-once. flush claims due outbox rows by pushing
next_attempt_at forward (a lease — no row locks are held across network
calls), sends them, then records the outcome. A worker crash mid-send
just means the lease expires and the batch is redelivered; Polar
deduplicates on each event's external_id, so redelivery can never
double-count.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.core.cache import cache
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger("metering.tasks")

_FLUSH_LOCK_KEY = "polar_flush_lock"
_FLUSH_LOCK_SECONDS = 55
_CLAIM_LEASE = timedelta(minutes=5)
_BATCH_SIZE = 200


# ── Outbox delivery ───────────────────────────────────────────────────────


def _claim_batch(now, limit=_BATCH_SIZE):
    """Lease a batch of due pending rows; returns detached model instances."""
    from apps.metering.models import PolarEventOutbox

    with transaction.atomic():
        qs = PolarEventOutbox.objects.filter(status=PolarEventOutbox.STATUS_PENDING).filter(
            Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now)
        )
        if connection.vendor == "postgresql":
            qs = qs.select_for_update(skip_locked=True)
        rows = list(qs.order_by("created_at")[:limit])
        if not rows:
            return []
        PolarEventOutbox.objects.filter(id__in=[r.id for r in rows]).update(
            next_attempt_at=now + _CLAIM_LEASE
        )
    return rows


def _ensure_customers(rows) -> set[str]:
    """Provision Polar customers for the batch. Returns the external ids
    that could NOT be provisioned (their rows are skipped this round)."""
    from django.contrib.auth import get_user_model

    from apps.metering import polar_client
    from apps.metering.models import PolarEventOutbox

    User = get_user_model()
    external_ids = {r.external_customer_id for r in rows}
    users = {str(u.id): u for u in User.objects.filter(id__in=external_ids)}

    failed: set[str] = set()
    for ext_id in external_ids:
        user = users.get(ext_id)
        if user is None:
            # User deleted before their events flushed — undeliverable.
            PolarEventOutbox.objects.filter(
                external_customer_id=ext_id, status=PolarEventOutbox.STATUS_PENDING
            ).update(status=PolarEventOutbox.STATUS_DEAD, last_error="user no longer exists")
            failed.add(ext_id)
            continue
        try:
            polar_client.ensure_customer(user)
        except polar_client.PolarRejected as exc:
            # Permanent: Polar refuses this customer (e.g. undeliverable
            # email domain — dev accounts on @example.com). Retrying can
            # never succeed, so dead-letter the rows instead of skipping
            # them forever; their usage stays on the local ledger.
            logger.warning("polar customer rejected for user %s: %s", ext_id, exc)
            PolarEventOutbox.objects.filter(
                external_customer_id=ext_id, status=PolarEventOutbox.STATUS_PENDING
            ).update(
                status=PolarEventOutbox.STATUS_DEAD,
                last_error=f"customer rejected: {exc}"[:2000],
            )
            failed.add(ext_id)
        except polar_client.PolarUnavailable:
            raise  # abort the whole flush round; retry later
    return failed


def _mark_sent(rows) -> None:
    from apps.metering.models import PolarEventOutbox

    PolarEventOutbox.objects.filter(id__in=[r.id for r in rows]).update(
        status=PolarEventOutbox.STATUS_SENT,
        sent_at=timezone.now(),
        last_error="",
    )


def _mark_failed(rows, error: str, *, permanent: bool) -> None:
    from apps.metering.models import PolarEventOutbox

    now = timezone.now()
    for row in rows:
        attempts = row.attempts + 1
        dead = permanent or attempts >= PolarEventOutbox.MAX_ATTEMPTS
        PolarEventOutbox.objects.filter(id=row.id).update(
            attempts=attempts,
            status=PolarEventOutbox.STATUS_DEAD if dead else PolarEventOutbox.STATUS_PENDING,
            next_attempt_at=None if dead else now + timedelta(seconds=min(2**attempts * 30, 3600)),
            last_error=error[:2000],
        )


def _send_rows(rows) -> None:
    """Send one claimed batch, bisecting on validation rejects so a single
    poison event cannot dead-letter its whole batch."""
    from apps.metering import polar_client

    if not rows:
        return
    try:
        inserted, duplicates = polar_client.ingest([r.payload for r in rows])
        if duplicates:
            logger.info("polar ingest: %d inserted, %d duplicates", inserted, duplicates)
        _mark_sent(rows)
    except polar_client.PolarRejected as exc:
        if len(rows) == 1:
            _mark_failed(rows, str(exc), permanent=True)
            return
        mid = len(rows) // 2
        _send_rows(rows[:mid])
        _send_rows(rows[mid:])
    except polar_client.PolarUnavailable as exc:
        _mark_failed(rows, str(exc), permanent=False)
        raise


def flush_outbox_once() -> dict:
    """Drain due outbox rows. Callable inline (dev) or from the task."""
    from apps.metering import polar_client

    sent = 0
    skipped = 0
    while True:
        now = timezone.now()
        rows = _claim_batch(now)
        if not rows:
            break
        try:
            failed_customers = _ensure_customers(rows)
        except polar_client.PolarUnavailable:
            logger.info("polar unavailable during customer provisioning; will retry")
            break
        sendable = [r for r in rows if r.external_customer_id not in failed_customers]
        skipped += len(rows) - len(sendable)
        try:
            _send_rows(sendable)
        except polar_client.PolarUnavailable:
            logger.info("polar unavailable during ingest; will retry")
            break
        sent += len(sendable)
    return {"sent": sent, "skipped": skipped}


@shared_task(name="apps.metering.tasks.flush_polar_events")
def flush_polar_events():
    """Deliver pending outbox rows to Polar (single-flight)."""
    from apps.metering.services.events import ingest_mode

    if ingest_mode() == "off":
        return {"sent": 0, "skipped": 0}
    if not cache.add(_FLUSH_LOCK_KEY, 1, _FLUSH_LOCK_SECONDS):
        return {"sent": 0, "skipped": 0, "locked": True}
    try:
        return flush_outbox_once()
    finally:
        try:
            cache.delete(_FLUSH_LOCK_KEY)
        except Exception:
            pass


# ── Cap notifications (off the LLM hot path) ─────────────────────────────


@shared_task(name="apps.metering.tasks.notify_cap_threshold")
def notify_cap_threshold(user_id: str, ntype: str):
    """Re-verify a spend-threshold crossing against the ledger and create
    the in-app notification once per billing period."""
    from django.contrib.auth import get_user_model

    from apps.metering.services.periods import billing_period_for
    from apps.metering.services.usage_reader import period_spent_usd
    from core.ai_tracking import AI_SPEND_WARN_RATIO, effective_ai_cap

    User = get_user_model()
    user = User.objects.filter(id=user_id).first()
    if user is None:
        return

    cap = effective_ai_cap(user)
    if cap <= 0:
        return
    period = billing_period_for(user)
    spent = period_spent_usd(user, period)
    resets = period.end.date().isoformat()

    if ntype == "ai_cap_exceeded":
        threshold = cap
        title = "AI allowance reached"
        body = (
            f"You have used your full AI allowance (${cap:.2f}) for this "
            f"billing period. AI features pause until {resets}."
        )
    elif ntype == "ai_cap_warning":
        threshold = cap * AI_SPEND_WARN_RATIO
        title = "Approaching your AI allowance"
        body = (
            f"You have used over {int(AI_SPEND_WARN_RATIO * 100)}% of your "
            f"${cap:.2f} AI allowance for this billing period. It resets on {resets}."
        )
    else:
        return

    if spent < threshold:
        return  # counter drift; ledger says the crossing has not happened

    try:
        from apps.notifications.models import Notification

        already = Notification.objects.filter(
            user=user, type=ntype, created_at__gte=period.start
        ).exists()
        if not already:
            Notification.objects.create(
                user=user,
                type=ntype,
                title=title,
                message=body,
                data={"cap_usd": cap, "spent_usd": round(spent, 4)},
                action_url="/settings",
            )
    except Exception:
        logger.debug("cap notification create failed", exc_info=True)


# ── Retention ─────────────────────────────────────────────────────────────


@shared_task(name="apps.metering.tasks.prune_ai_usage_ledger")
def prune_ai_usage_ledger():
    """Prune ledger rows older than AI_USAGE_RETENTION_DAYS — but only
    once Polar is the authoritative read source."""
    from django.conf import settings

    from apps.accounts.models import AITokenUsage

    if not getattr(settings, "POLAR_READS_ENABLED", False):
        logger.info("ledger pruning skipped: POLAR_READS_ENABLED is off")
        return {"deleted": 0}

    cutoff = timezone.now() - timedelta(days=settings.AI_USAGE_RETENTION_DAYS)
    deleted = 0
    while True:
        batch = list(
            AITokenUsage.objects.filter(created_at__lt=cutoff).values_list("id", flat=True)[:10000]
        )
        if not batch:
            break
        deleted += AITokenUsage.objects.filter(id__in=batch).delete()[0]
    return {"deleted": deleted}


@shared_task(name="apps.metering.tasks.prune_polar_outbox")
def prune_polar_outbox():
    """Drop delivered outbox rows after 30 days; dead rows after 90."""
    from apps.metering.models import PolarEventOutbox

    now = timezone.now()
    sent_deleted = PolarEventOutbox.objects.filter(
        status=PolarEventOutbox.STATUS_SENT, sent_at__lt=now - timedelta(days=30)
    ).delete()[0]
    dead_deleted = PolarEventOutbox.objects.filter(
        status=PolarEventOutbox.STATUS_DEAD, updated_at__lt=now - timedelta(days=90)
    ).delete()[0]
    return {"sent_deleted": sent_deleted, "dead_deleted": dead_deleted}


# ── Prompt runs ───────────────────────────────────────────────────────────


@shared_task(name="apps.metering.tasks.release_stale_prompt_runs")
def release_stale_prompt_runs():
    """Give back prompt runs a worker reserved and never settled."""
    from apps.metering.services import prompt_runs

    return {"released": prompt_runs.release_stale()}
