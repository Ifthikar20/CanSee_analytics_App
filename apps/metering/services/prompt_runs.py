"""Prompt runs: the free plan's monthly allowance.

One prompt run is one prompt answered by one AI engine. It counts when the
engine gives a successful, non-empty answer; a failure, a refusal, an
empty answer or a denied call gives it back. The free plan gets
``FREE_MONTHLY_PROMPT_RUNS`` (20) a billing period, shared by every
project and every engine of the account. Paid plans are unlimited here
(their rows are still written, so usage can be shown).

Every place that asks an engine goes through ``guard`` (or ``reserve`` and
``Reservation.settle``) around its retry loop:

  - reserve takes one unit with a conditional UPDATE on the account's
    UsageCounter row (``value < limit``), so concurrent workers can never
    pass the limit;
  - an ``idempotency_key`` makes a redelivered task reuse its reservation
    instead of taking a second unit;
  - settle(answered) marks the run counted, or releases it and gives the
    unit back. The release is a conditional UPDATE too, so a late settle
    and the stale-reservation sweep can never both give a unit back.

The account is the project owner's (``website.user``), or the
organization's while organization features are on: the project's own
organization for an organization project, else the owner's. The plan comes from
core.ai_tracking.ai_allowance_plan, the resolver of the AI spend wall, so
PAYWALL_ENABLED does not lift the limit.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import Count, F
from django.utils import timezone

logger = logging.getLogger("metering.prompt_runs")

# The error a denied engine call returns (ProviderResult.error). It must not
# contain "allowance": the engine error mappers read that as the dollar wall.
DENIAL = "prompt_runs_exhausted: this month's free prompt runs are used up"
DENIAL_PREFIX = "prompt_runs_exhausted"
CHECK_FAILED = "prompt_runs_check_failed: prompt runs could not be checked"

CODE_EXHAUSTED = "prompt_runs_exhausted"
CODE_INSUFFICIENT = "insufficient_prompt_runs"

# Where an answer was asked from (PromptRun.feature).
FEATURES = (
    "audit", "prompt_crawl", "scheduled_crawl", "agent_research", "autopilot",
    "country_check", "prompt_box", "brand_research", "shopping", "smoke_test",
)

SCOPE_ACCOUNT = "account"
SCOPE_ORGANIZATION = "organization"


def free_limit() -> int:
    """Prompt runs a free account gets each billing period."""
    from core.utils.constants import PLAN_LIMITS, Plan

    default = PLAN_LIMITS[Plan.FREE]["monthly_prompt_runs"]
    try:
        return max(0, int(getattr(settings, "FREE_MONTHLY_PROMPT_RUNS", default)))
    except (TypeError, ValueError):
        return default


def stale_minutes() -> int:
    return max(1, int(getattr(settings, "PROMPT_RUN_STALE_MINUTES", 30)))


def engine_key(provider: str) -> str:
    """The public engine key of a provider or engine key (gpt4 -> chatgpt)."""
    from apps.citations.services.url_analytics import model_key

    return model_key(str(provider or "").split(":", 1)[0])[:16]


# -- account ------------------------------------------------------------------


@dataclass(frozen=True)
class Account:
    key: str
    owner: object
    plan: str
    limit: int  # -1 is unlimited
    scope: str
    period_start: object
    period_end: object

    @property
    def period_key(self) -> str:
        return self.period_start.date().isoformat()

    @property
    def unlimited(self) -> bool:
        return self.limit < 0


def owner_for(user, website=None):
    """The user whose account an answer is counted on: the project owner."""
    owner = getattr(website, "user", None) if website is not None else None
    return owner if owner is not None else user


def _project_org(website):
    """The organization a project belongs to, while organization features
    are on; None for a personal project."""
    from core.permissions.org import org_features_enabled

    if website is None or not getattr(website, "organization_id", None):
        return None
    if not org_features_enabled():
        return None
    return website.organization


def account_for(user, website=None) -> Account | None:
    """The account an answer for ``user`` on ``website`` counts on, or None
    for an unattributed call (no user)."""
    from apps.billing.services.plan_limits import org_plan_for, plan_for_org
    from apps.metering.services.periods import billing_period_for, month_bounds
    from core.ai_tracking import ai_allowance_plan
    from core.utils.constants import PLAN_LIMITS, Plan

    owner = owner_for(user, website)
    if owner is None or not getattr(owner, "pk", None):
        return None
    org = _project_org(website)
    if org is not None:
        # An organization's project counts on that organization, also after
        # its creator left it or when the creator's first one is another.
        plan = plan_for_org(org)
        plan = plan if plan in PLAN_LIMITS else Plan.FREE
    else:
        plan = str(ai_allowance_plan(owner))
        org = getattr(owner, "_org_cache", None) if org_plan_for(owner) is not None else None
    raw = PLAN_LIMITS.get(plan, PLAN_LIMITS[Plan.FREE]).get("monthly_prompt_runs", -1)
    limit = free_limit() if plan == Plan.FREE else (int(raw) if isinstance(raw, int) else -1)
    if org is not None:
        # An organization shares one count; members' own subscriptions do
        # not move its window, so it is the calendar month.
        start, end = month_bounds()
        return Account(f"org:{org.id}", owner, plan, limit, SCOPE_ORGANIZATION, start, end)
    period = billing_period_for(owner)
    return Account(f"user:{owner.pk}", owner, plan, limit, SCOPE_ACCOUNT,
                   period.start, period.end)


def limit_for(user, website=None) -> int:
    """Prompt runs a period for the account (-1 is unlimited)."""
    account = account_for(user, website)
    return -1 if account is None else account.limit


def account_key_for(user, website=None) -> str:
    account = account_for(user, website)
    return "" if account is None else account.key


def is_limited(user, website=None) -> bool:
    """True when the account has a prompt-run limit (the free plan). These
    accounts never see dollar amounts."""
    try:
        account = account_for(user, website)
    except Exception:
        logger.warning("prompt runs: account lookup failed", exc_info=True)
        return False
    return account is not None and not account.unlimited


def _counter_value(account: Account) -> int:
    from apps.metering.models import UsageCounter

    return (
        UsageCounter.objects.filter(
            meter=UsageCounter.METER_PROMPT_RUNS, account_key=account.key,
            period_key=account.period_key,
        ).values_list("value", flat=True).first()
        or 0
    )


def remaining_for(account: Account | None) -> int | None:
    """Runs left this period; None when unlimited or unattributed."""
    if account is None or account.unlimited:
        return None
    return max(0, account.limit - _counter_value(account))


# -- reserve and settle ---------------------------------------------------------


@dataclass
class Reservation:
    granted: bool
    denial: str = ""
    run_id: object = None
    _settled: bool = field(default=False, repr=False)

    def settle(self, answered: bool) -> None:
        """Count the run (a successful, non-empty answer) or give it back.
        Safe to call more than once; never raises."""
        if self._settled or self.run_id is None:
            self._settled = True
            return
        self._settled = True
        try:
            settle(self.run_id, answered=answered)
        except Exception:
            logger.warning("prompt runs: settle failed for %s", self.run_id, exc_info=True)


_UNATTRIBUTED = Reservation(granted=True)


class _Replayed(Exception):
    """A concurrent reservation took the same idempotency key first."""


def _take_unit(account: Account) -> bool:
    """One unit of the account's counter: a conditional increment for a
    limited plan (False when the limit is reached)."""
    from apps.metering.models import UsageCounter

    counter, _ = UsageCounter.objects.get_or_create(
        meter=UsageCounter.METER_PROMPT_RUNS, account_key=account.key,
        period_key=account.period_key,
    )
    rows = UsageCounter.objects.filter(pk=counter.pk)
    if not account.unlimited:
        rows = rows.filter(value__lt=account.limit)
    return rows.update(value=F("value") + 1, updated_at=timezone.now()) == 1


def _give_back(account_key: str, period_key: str) -> None:
    from apps.metering.models import UsageCounter

    UsageCounter.objects.filter(
        meter=UsageCounter.METER_PROMPT_RUNS, account_key=account_key, period_key=period_key,
        value__gt=0,
    ).update(value=F("value") - 1, updated_at=timezone.now())


def reserve(*, user, website=None, engine: str, feature: str,
            idempotency_key: str | None = None) -> Reservation:
    """Hold one prompt run before an engine is asked. Never raises.

    Denied when a limited account has none left; a database error denies a
    limited account (fail-closed, as the AI spend wall does) and lets an
    unlimited one through."""
    from apps.metering.models import PromptRun

    try:
        account = account_for(user, website)
    except Exception:
        logger.warning("prompt runs: account lookup failed; denying", exc_info=True)
        return Reservation(granted=False, denial=CHECK_FAILED)
    if account is None:
        return _UNATTRIBUTED
    key = (idempotency_key or "")[:128] or None
    fields = {
        "account_key": account.key, "period_key": account.period_key,
        "user": user if getattr(user, "pk", None) else None,
        "website": website if getattr(website, "pk", None) else None,
        "engine": engine_key(engine), "feature": str(feature or "")[:32],
    }
    for _attempt in (1, 2):
        try:
            with transaction.atomic():
                existing = None
                if key:
                    existing = PromptRun.objects.select_for_update().filter(
                        idempotency_key=key,
                    ).first()
                    if existing is not None and existing.status != PromptRun.STATUS_RELEASED:
                        # A retry of a call already holding (or counted as) a run.
                        return Reservation(granted=True, run_id=existing.id)
                if not _take_unit(account):
                    return Reservation(granted=False, denial=DENIAL)
                if existing is not None:
                    existing.status = PromptRun.STATUS_RESERVED
                    existing.account_key, existing.period_key = account.key, account.period_key
                    existing.settled_at = None
                    existing.save(update_fields=["status", "account_key", "period_key",
                                                 "settled_at"])
                    return Reservation(granted=True, run_id=existing.id)
                try:
                    with transaction.atomic():
                        run = PromptRun.objects.create(idempotency_key=key, **fields)
                except IntegrityError:
                    raise _Replayed() from None
                return Reservation(granted=True, run_id=run.id)
        except _Replayed:
            continue  # the unit taken above was rolled back; read the winner's row
        except DatabaseError:
            logger.warning("prompt runs: reserve failed", exc_info=True)
            if account.unlimited:
                return _UNATTRIBUTED
            return Reservation(granted=False, denial=CHECK_FAILED)
    return Reservation(granted=False, denial=CHECK_FAILED)


def settle(run_id, *, answered: bool) -> None:
    """Counted or released. A run the stale sweep released while its call
    was still out is counted again when the answer does arrive."""
    from apps.metering.models import PromptRun

    now = timezone.now()
    with transaction.atomic():
        run = PromptRun.objects.select_for_update().filter(id=run_id).first()
        if run is None:
            return
        if answered:
            if run.status == PromptRun.STATUS_RESERVED:
                PromptRun.objects.filter(id=run.id).update(
                    status=PromptRun.STATUS_COUNTED, settled_at=now)
            elif run.status == PromptRun.STATUS_RELEASED:
                from apps.metering.models import UsageCounter

                counter, _ = UsageCounter.objects.get_or_create(
                    meter=UsageCounter.METER_PROMPT_RUNS, account_key=run.account_key,
                    period_key=run.period_key,
                )
                # The engine answered, so the run is real even past the limit.
                UsageCounter.objects.filter(pk=counter.pk).update(value=F("value") + 1)
                PromptRun.objects.filter(id=run.id).update(
                    status=PromptRun.STATUS_COUNTED, settled_at=now)
            return
        if run.status == PromptRun.STATUS_RESERVED:
            PromptRun.objects.filter(id=run.id).update(
                status=PromptRun.STATUS_RELEASED, settled_at=now)
            _give_back(run.account_key, run.period_key)


def answered(result) -> bool:
    """A successful, non-empty answer (the only outcome that counts)."""
    return bool(
        result is not None
        and getattr(result, "succeeded", False)
        and str(getattr(result, "text", "") or "").strip()
    )


def is_denial(error: str) -> bool:
    return str(error or "").strip().lower().startswith(DENIAL_PREFIX)


def guard(call, *, user, website=None, engine: str, feature: str, key: str | None = None):
    """Reserve one run, make ``call()`` (the whole retry loop), settle.

    Returns what ``call`` returned, or a ProviderResult with the denial
    when no run is left (the engine is not asked)."""
    reservation = reserve(user=user, website=website, engine=engine, feature=feature,
                          idempotency_key=key)
    if not reservation.granted:
        from core.llm.base import ProviderResult

        return ProviderResult(succeeded=False, error=reservation.denial)
    result = None
    try:
        result = call()
        return result
    finally:
        reservation.settle(answered(result))


def release_stale(older_than_minutes: int | None = None) -> int:
    """Release reservations left open past the stale window (a worker that
    died mid-call). Returns how many were released."""
    from apps.metering.models import PromptRun

    cutoff = timezone.now() - timedelta(minutes=older_than_minutes or stale_minutes())
    released = 0
    ids = list(
        PromptRun.objects.filter(status=PromptRun.STATUS_RESERVED, created_at__lt=cutoff)
        .values_list("id", flat=True)[:1000]
    )
    for run_id in ids:
        with transaction.atomic():
            run = PromptRun.objects.select_for_update().filter(
                id=run_id, status=PromptRun.STATUS_RESERVED,
            ).first()
            if run is None:
                continue
            PromptRun.objects.filter(id=run.id).update(
                status=PromptRun.STATUS_RELEASED, settled_at=timezone.now())
            _give_back(run.account_key, run.period_key)
            released += 1
    return released


# -- status and pre-checks ---------------------------------------------------------


def status(user, website=None) -> dict:
    """The runs block every surface shows (same shape everywhere):

    ``{unlimited, limit, used, reserved, remaining, plan, scope,
    period_start, resets_at, by_engine, by_feature}``. ``used`` counts
    answered runs; ``reserved`` the runs out right now; ``limit`` and
    ``remaining`` are null when unlimited.
    """
    from apps.metering.models import PromptRun

    account = account_for(user, website)
    if account is None:
        now = timezone.now()
        return {
            "unlimited": True, "limit": None, "used": 0, "reserved": 0, "remaining": None,
            "plan": "", "scope": SCOPE_ACCOUNT, "period_start": now.isoformat(),
            "resets_at": now.isoformat(), "by_engine": {}, "by_feature": {},
        }
    rows = PromptRun.objects.filter(account_key=account.key, period_key=account.period_key)
    by_status = dict(rows.values_list("status").annotate(n=Count("id")).values_list("status", "n"))
    counted = rows.filter(status=PromptRun.STATUS_COUNTED)
    by_engine = dict(counted.values_list("engine").annotate(n=Count("id"))
                     .values_list("engine", "n"))
    by_feature = dict(counted.values_list("feature").annotate(n=Count("id"))
                      .values_list("feature", "n"))
    return {
        "unlimited": account.unlimited,
        "limit": None if account.unlimited else account.limit,
        "used": int(by_status.get(PromptRun.STATUS_COUNTED, 0)),
        "reserved": int(by_status.get(PromptRun.STATUS_RESERVED, 0)),
        "remaining": remaining_for(account),
        "plan": account.plan,
        "scope": account.scope,
        "period_start": account.period_start.isoformat(),
        "resets_at": account.period_end.isoformat(),
        "by_engine": {k: int(v) for k, v in sorted(by_engine.items())},
        "by_feature": {k: int(v) for k, v in sorted(by_feature.items())},
    }


def available(user, website=None, needed: int = 1) -> tuple[bool, int | None]:
    """(whether ``needed`` runs are left, runs left or None when unlimited)."""
    account = account_for(user, website)
    remaining = remaining_for(account)
    if remaining is None:
        return True, None
    return remaining >= max(0, int(needed)), remaining


def estimate_block(user, website=None, needed: int = 0) -> dict:
    """``{needed, remaining, limit, unlimited, enough, resets_at}`` for an
    estimate (``remaining``, ``limit`` and ``resets_at`` null when unlimited)."""
    account = account_for(user, website)
    remaining = remaining_for(account)
    unlimited = remaining is None
    return {
        "needed": int(needed),
        "remaining": remaining,
        "limit": None if unlimited else account.limit,
        "unlimited": unlimited,
        "enough": unlimited or remaining >= int(needed),
        "resets_at": None if unlimited else account.period_end.isoformat(),
    }


def _renews(account: Account) -> str:
    end = account.period_end
    return f"{end:%B} {end.day}"


def _noun(account: Account, n: int) -> str:
    free = "free " if account.plan == "free" else ""
    return f"{free}prompt run{'' if n == 1 else 's'}"


def shortfall(user, website=None, needed: int = 1) -> dict | None:
    """The blocker for ``needed`` runs, or None when they are left:
    ``{code, message, status, details}``."""
    account = account_for(user, website)
    remaining = remaining_for(account)
    needed = max(0, int(needed))
    if remaining is None or remaining >= needed or needed == 0:
        return None
    details = {"needed": needed, "remaining": remaining, "limit": account.limit,
               "resets_at": account.period_end.isoformat()}
    if remaining <= 0:
        return {
            "code": CODE_EXHAUSTED, "status": 403, "details": details,
            "message": (f"You have used this month's {account.limit} "
                        f"{_noun(account, account.limit)}. They renew on {_renews(account)}."),
        }
    return {
        "code": CODE_INSUFFICIENT, "status": 403, "details": details,
        "message": (f"This needs {needed} {_noun(account, needed)} and {remaining} of your "
                    f"{account.limit} are left this month. Choose fewer prompts or engines."),
    }


def require(user, website=None, needed: int = 1) -> None:
    """Raise ``insufficient_prompt_runs`` or ``prompt_runs_exhausted`` (403,
    details {needed, remaining, limit, resets_at}) when ``needed`` runs are
    not left. Unlimited accounts and unattributed calls pass."""
    from core.exceptions import CanseeException

    block = shortfall(user, website, needed)
    if block is not None:
        raise CanseeException(block["message"], code=block["code"],
                              status_code=block["status"], details=block["details"])


# Dollar-free words for the hidden AI spend backstop (free accounts).
ALLOWANCE_USED_FREE = "This month's free AI allowance is used up. It renews next month."
ALLOWANCE_SHORT_FREE = ("This needs more of this month's free AI allowance than is left. "
                        "Choose fewer prompts or engines.")
