"""Who may start a run now: configuration, allowance, rate and concurrency.

All of these are checked before anything is written or sent, so a denied
POST never reaches the model.
"""
from __future__ import annotations

import hashlib

from django.db import connection

from apps.agent.conf import agent_conf, api_key_configured
from apps.agent.constants import ACTIVE_RUN_STATUSES
from apps.agent.exceptions import (
    AgentBusy,
    AgentNotConfigured,
    AllowanceExceeded,
    RunInProgress,
)
from core.llm import allowance_denial
from core.resilience import TokenBucket

SEND_BUCKET_CAPACITY = 10
SEND_BUCKET_REFILL_PER_SECOND = 0.2


def check_configured() -> None:
    if not api_key_configured():
        raise AgentNotConfigured()


def check_allowance(user) -> None:
    if allowance_denial(user):
        from core.ai_tracking import effective_ai_cap, month_to_date_cost

        raise AllowanceExceeded(cap_usd=float(effective_ai_cap(user)),
                                spent_usd=float(month_to_date_cost(user)))


def send_bucket(user) -> TokenBucket:
    return TokenBucket(f"agent-send:{user.id}", capacity=SEND_BUCKET_CAPACITY,
                       refill_per_second=SEND_BUCKET_REFILL_PER_SECOND)


def check_send_rate(user) -> None:
    if not send_bucket(user).try_acquire():
        raise AgentBusy("You are sending messages faster than the agent can take them. "
                        "Wait a few seconds and try again.")


def _lock_key(user) -> int:
    digest = hashlib.blake2b(f"agent-user:{user.id}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def lock_user(user) -> None:
    """Serialize run creation for one user until the transaction ends."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [_lock_key(user)])


def check_chat_free(chat) -> None:
    from apps.agent.models import AgentRun

    active = (AgentRun.objects.filter(chat=chat, status__in=ACTIVE_RUN_STATUSES)
              .values_list("id", flat=True).first())
    if active:
        raise RunInProgress(run_id=active)


def check_user_slots(user) -> None:
    from apps.agent.models import AgentRun

    active = AgentRun.objects.filter(user=user, status__in=ACTIVE_RUN_STATUSES).count()
    if active >= agent_conf().user_active_runs:
        raise AgentBusy()
