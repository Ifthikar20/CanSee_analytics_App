"""Autopilot settings (contract 3a and 3b, backend 4b).

``get_or_build`` returns a project's row, or an unsaved default while it
has none (the payload says ``saved: false``). ``save`` validates a PUT with
the contract's words and keeps the consent record of the daily text: only
the number's owner can turn the text on for it, any member can turn it
off. ``payload`` is the settings page's one read. ``covers`` tells the
older per-prompt and audit schedules which work Autopilot does instead.

The account of a project is its owner (``website.user``): the plan, the
tracked-prompt cap and the AI allowance are always the owner's, whoever
makes the request.
"""

from __future__ import annotations

import logging
import math
import re
import uuid
import zoneinfo
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from functools import lru_cache

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.autopilot.models import CADENCES, Autopilot, AutopilotRun, default_google_cadence
from core.exceptions import CanseeException

logger = logging.getLogger("apps")

ZERO = Decimal("0")
OPTIN_INTERVAL = timedelta(hours=24)
# The dispatcher's beat interval (config/celery.py "autopilot-dispatch").
TICK_MINUTES = 5

QUARTER_HOUR = "Choose a time on the quarter hour, like 07:00."
TIMEZONE_MESSAGE = "Choose a time zone from the list."
CADENCE_MESSAGE = "Choose every day or once a week."
QUIET_MESSAGE = "Quiet hours need a start and an end that differ."
NOTHING_ON = "Choose at least one thing for Autopilot to run."
RECIPIENT_MESSAGE = "Choose a mobile number you verified."
CONSENT_MESSAGE = "Agree to the text message terms to turn on the daily text."
PLAN_REQUIRED = "Autopilot is part of the 100 and 200 prompt plans."
BODY_MESSAGE = "Send a JSON object."
CONSENT_TEXT = (
    "I agree to receive one automated text a day from Cansee about {project} at this "
    "number. Msg & data rates may apply. Reply STOP to cancel or HELP for help. Consent "
    "is not a condition of purchase."
)

BOOL_FIELDS = ("enabled", "run_engines", "run_google", "run_analysis", "sms_digest")
TIME_FIELDS = ("run_time", "quiet_start", "quiet_end")
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::00)?$")

# Fallbacks for settings the settings module always defines (backend 1).
_DEFAULTS = {
    "AUTOPILOT_ENABLED": True,
    "AUTOPILOT_RUN_MODE": "auto",
    "AUTOPILOT_MAX_ACTIVE_RUNS": 2,
    "AUTOPILOT_PROMPT_CONCURRENCY": 3,
    "AUTOPILOT_STEP_SECONDS": 1200,
    "AUTOPILOT_STALE_MINUTES": 30,
    "AUTOPILOT_RESERVE_PCT": 10,
    "AUTOPILOT_GOOGLE_REUSE_HOURS": 20,
    "AUTOPILOT_GOOGLE_WEEKLY_REUSE_HOURS": 164,
    "AUTOPILOT_GOOGLE_PAGES_PER_PROJECT": 200,
    "AUTOPILOT_GOOGLE_PAGES_PER_DAY": 1000,
    "AUTOPILOT_GOOGLE_CONCURRENCY": 2,
    "AUTOPILOT_ANALYSIS_WAIT_MINUTES": 15,
    "AUTOPILOT_MIN_RANK_ANSWERS": 4,
}


def conf(name: str):
    return getattr(settings, name, _DEFAULTS[name])


def enabled_on_server() -> bool:
    return bool(conf("AUTOPILOT_ENABLED"))


def min_rank_answers() -> int:
    return max(1, int(conf("AUTOPILOT_MIN_RANK_ANSWERS")))


def reserve_pct() -> int:
    """The share of the AI allowance Autopilot leaves for the user (0 to 100)."""
    return min(100, max(0, int(conf("AUTOPILOT_RESERVE_PCT"))))


def invalid(field: str, message: str) -> CanseeException:
    return CanseeException(
        message, code="invalid", status_code=400, details={"field": field, "message": message},
    )


def plan_required() -> CanseeException:
    return CanseeException(PLAN_REQUIRED, code="plan_required", status_code=403)


def plan_allows(user, feature: str) -> bool:
    from apps.billing.services import plan_limits

    return plan_limits.plan_allows(user, feature)


# -- formatting --------------------------------------------------------------------


def usd4(value) -> str:
    return str(Decimal(str(value or 0)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def usd2(value, rounding=ROUND_HALF_UP) -> str:
    return str(Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=rounding))


def iso(value) -> str | None:
    """ISO 8601 UTC with a Z, to the millisecond."""
    if value is None:
        return None
    text = value.astimezone(UTC).isoformat()
    if value.microsecond:
        text = text[:23] + text[26:]
    return text.replace("+00:00", "Z")


def hhmm(value: time | None) -> str | None:
    return value.strftime("%H:%M") if value is not None else None


def month_day(day: date) -> str:
    """"Dec 1"."""
    return f"{day:%b} {day.day}"


def user_name(user) -> str | None:
    if user is None:
        return None
    return (getattr(user, "full_name", "") or "").strip() or user.email.split("@")[0]


# -- time -----------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _zone_names() -> frozenset:
    return frozenset(zoneinfo.available_timezones())


def known_zone(name) -> bool:
    return isinstance(name, str) and name in _zone_names()


def zone(name) -> zoneinfo.ZoneInfo:
    """The project's time zone; an unknown one reads as UTC."""
    if known_zone(name):
        try:
            return zoneinfo.ZoneInfo(name)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            pass
    return zoneinfo.ZoneInfo("UTC")


def local_now(autopilot, now: datetime | None = None) -> datetime:
    return (now or timezone.now()).astimezone(zone(autopilot.timezone))


def local_today(autopilot, now: datetime | None = None) -> date:
    return local_now(autopilot, now).date()


def at_local(day: date, wall: time, tz) -> datetime:
    """The UTC instant of a local wall-clock time (the earlier one when a
    clock change repeats it)."""
    return datetime.combine(day, wall, tzinfo=tz).astimezone(UTC)


def next_tick(now: datetime) -> datetime:
    """The dispatcher's next beat after ``now``."""
    base = now.replace(second=0, microsecond=0)
    base -= timedelta(minutes=base.minute % TICK_MINUTES)
    return base + timedelta(minutes=TICK_MINUTES)


def parse_time(value) -> time | None:
    """A quarter-hour "HH:MM", or None."""
    if not isinstance(value, str):
        return None
    match = _TIME_RE.match(value.strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute not in (0, 15, 30, 45):
        return None
    return time(hour, minute)


def in_quiet_hours(autopilot, now: datetime | None = None) -> bool:
    start, end = autopilot.quiet_start, autopilot.quiet_end
    if start == end:
        return False
    wall = local_now(autopilot, now).time().replace(tzinfo=None)
    if start < end:
        return start <= wall < end
    return wall >= start or wall < end


def quiet_end_after(autopilot, now: datetime | None = None) -> datetime:
    """The next end of quiet hours after ``now``, in UTC."""
    tz = zone(autopilot.timezone)
    now = now or timezone.now()
    local = now.astimezone(tz)
    when = at_local(local.date(), autopilot.quiet_end, tz)
    if when <= now:
        when = at_local(local.date() + timedelta(days=1), autopilot.quiet_end, tz)
    return when


# -- rows ---------------------------------------------------------------------------


def build_default(website) -> Autopilot:
    return Autopilot(website=website, google_cadence=default_google_cadence())


def get_or_build(website) -> Autopilot:
    """The project's settings, or an unsaved default (``saved: false``)."""
    autopilot = (
        Autopilot.objects.select_related("sms_subscription", "sms_subscription__user")
        .filter(website=website).first()
    )
    return autopilot if autopilot is not None else build_default(website)


def is_saved(autopilot) -> bool:
    return not autopilot._state.adding


def will_run(autopilot, website) -> bool:
    """Autopilot runs this project: on, not paused, on this server, and the
    owner's plan includes it."""
    return bool(
        enabled_on_server()
        and is_saved(autopilot)
        and autopilot.enabled
        and not autopilot.paused_reason
        and plan_allows(website.user, "autopilot")
    )


def covers(website) -> frozenset:
    """The work Autopilot does for ``website`` today, which the older
    schedules skip: ``engines`` (the AI crawl and the audit) and ``google``
    (the Google recheck). Empty while Autopilot is off, paused, turned off
    on this server or not on the owner's plan."""
    autopilot = Autopilot.objects.filter(website=website).first()
    if autopilot is None or not will_run(autopilot, website):
        return frozenset()
    out = set()
    if autopilot.run_engines:
        out.add("engines")
    if autopilot.run_google:
        out.add("google")
    return frozenset(out)


def next_run_at(autopilot, website, now: datetime | None = None) -> datetime | None:
    """When the dispatcher next starts a run (backend 4e): today's run time,
    the next dispatcher tick once that time has passed with no run today,
    or tomorrow's run time once today's run exists. None while it will not
    run."""
    if not will_run(autopilot, website):
        return None
    now = now or timezone.now()
    tz = zone(autopilot.timezone)
    local = now.astimezone(tz)
    today = local.date()
    if AutopilotRun.objects.filter(website=website, local_date=today).exists():
        return at_local(today + timedelta(days=1), autopilot.run_time, tz)
    if local.time().replace(tzinfo=None) < autopilot.run_time:
        return at_local(today, autopilot.run_time, tz)
    return next_tick(now)


# -- allowance ------------------------------------------------------------------------


def allowance_state(user) -> dict:
    """The owner's AI allowance with Autopilot's line: Autopilot spends up to
    ``line`` (the cap less the reserve); ``usable`` is what is left under it.
    ``line`` and ``usable`` are None when the allowance is unlimited."""
    from apps.insights.services import runs as insights_runs
    from apps.metering.services.periods import billing_period_for

    a = insights_runs.allowance(user)
    pct = reserve_pct()
    period = billing_period_for(user)
    line = usable = None
    if not a["unlimited"]:
        line = a["cap"] * Decimal(100 - pct) / Decimal(100)
        usable = max(ZERO, line - a["spent"])
    return {
        **a,
        "line": line,
        "usable": usable,
        "reserve_pct": pct,
        "period": period,
        "resets_on": period.end.astimezone(UTC).date(),
    }


def past_line(state: dict) -> bool:
    """The owner's spend reached Autopilot's line."""
    return not state["unlimited"] and state["spent"] >= state["line"]


def allowance_payload(state: dict) -> dict:
    unlimited = state["unlimited"]
    return {
        "unlimited": unlimited,
        "cap_usd": None if unlimited else usd2(state["cap"]),
        "spent_usd": usd2(state["spent"]),
        "left_usd": None if unlimited else usd2(state["left"], ROUND_DOWN),
        "reserve_pct": state["reserve_pct"],
        "resets_on": state["resets_on"].isoformat(),
    }


# -- payload --------------------------------------------------------------------------


def plan_block(user) -> dict:
    """The owner's resolved plan (the one its limits come from: Business for
    every account while the paywall is off)."""
    from apps.billing.services.plan_limits import _resolve_plan_key
    from core.utils.constants import PLAN_NAMES

    plan = str(_resolve_plan_key(user))
    return {
        "id": plan,
        "name": PLAN_NAMES.get(plan, plan.replace("_", " ").capitalize()),
        "autopilot": plan_allows(user, "autopilot"),
        "sms_digest": plan_allows(user, "sms_digest"),
    }


def tracked_block(website) -> dict:
    from apps.billing.services import tracked_prompts

    owner = website.user
    _runnable, over = tracked_prompts.runnable_brand_prompts(website)
    capacity = tracked_prompts.capacity(owner)
    return {"limit": capacity["limit"], "used": capacity["used"], "over_limit": over}


def settings_block(autopilot) -> dict:
    return {
        "enabled": autopilot.enabled,
        "run_time": hhmm(autopilot.run_time),
        "timezone": autopilot.timezone,
        "run_engines": autopilot.run_engines,
        "run_google": autopilot.run_google,
        "google_cadence": autopilot.google_cadence,
        "run_analysis": autopilot.run_analysis,
        "sms_digest": autopilot.sms_digest,
        "sms_subscription_id": (
            str(autopilot.sms_subscription_id) if autopilot.sms_subscription_id else None
        ),
        "quiet_start": hhmm(autopilot.quiet_start),
        "quiet_end": hhmm(autopilot.quiet_end),
        "paused_reason": autopilot.paused_reason or "",
    }


def engines_block() -> list[dict]:
    from apps.agent.tools.research import engine_catalog

    return [
        {"key": e["key"], "label": e["label"], "configured": e["configured"]}
        for e in engine_catalog()
    ]


def google_block() -> dict:
    from apps.citations.services import serp_google

    which = serp_google.provider()
    scraper = which == serp_google.PROVIDER_SCRAPERAPI
    return {
        "configured": bool(which),
        "provider_label": serp_google.PROVIDER_LABELS.get(which),
        "credits_per_page": int(settings.SCRAPERAPI_CREDITS_PER_SEARCH) if scraper else None,
    }


def consent_text(website) -> str:
    return CONSENT_TEXT.format(project=website.name)


def sms_block(autopilot, website, user) -> dict:
    from apps.notifications.api.v1.sms_views import _masked_phone
    from apps.notifications.models import SmsSubscription
    from apps.notifications.services import sms_service

    numbers = [
        {"id": str(sub.id), "phone_masked": _masked_phone(sub.phone_e164), "yours": True}
        for sub in SmsSubscription.objects.filter(
            user=user, status=SmsSubscription.Status.VERIFIED,
        ).order_by("-created_at")
    ]
    recipient = None
    sub = autopilot.sms_subscription if autopilot.sms_subscription_id else None
    if sub is not None:
        recipient = {
            "id": str(sub.id),
            "phone_masked": _masked_phone(sub.phone_e164),
            "status": sub.status,
            "set_by_name": user_name(sub.user),
        }
    return {
        "available": sms_service.is_enabled(),
        "numbers": numbers,
        "recipient": recipient,
        "consent_text": consent_text(website),
        "consent_version": Autopilot.CONSENT_VERSION,
    }


def _days(period) -> int:
    return max(1, round((period.end - period.start).total_seconds() / 86400))


def cost_block(autopilot, website, state: dict) -> dict:
    """The last run's real cost, the month it projects, when the allowance
    runs out at that rate, and the bound of one run before the first."""
    from apps.autopilot.services import pipeline

    last = pipeline.last_measured_run(website)
    bound = pipeline.run_bound(autopilot, website)
    out = {
        "last_run": None,
        "month_projection_usd": None,
        "runs_out_on": None,
        "bound": {"ai_usd_max": usd4(bound["ai_usd_max"]), "google_pages_max": bound["pages_max"]},
    }
    if last is None:
        return out
    out["last_run"] = {
        "ai_usd": usd4(last.ai_cost_usd),
        "answers": last.answers_asked,
        "google_pages": last.google_pages,
        "credits": pipeline.credits_for(last),
    }
    daily = Decimal(str(last.ai_cost_usd or 0))
    out["month_projection_usd"] = usd2(daily * _days(state["period"]))
    if not state["unlimited"] and daily > 0:
        today = timezone.now().astimezone(UTC).date()
        room = state["line"] - state["spent"]
        runs_out = today if room <= 0 else today + timedelta(days=math.ceil(room / daily))
        if runs_out < state["resets_on"]:
            out["runs_out_on"] = runs_out.isoformat()
    return out


def payload(website, user) -> dict:
    """The settings page's one read (contract 3a)."""
    from apps.autopilot.services import pipeline

    autopilot = get_or_build(website)
    state = allowance_state(website.user)
    today = local_today(autopilot)
    recent = AutopilotRun.objects.filter(website=website).select_related("analysis_run")
    runs = list(recent.order_by("-created_at")[:7])
    today_run = next((r for r in runs if r.local_date == today), None)
    if today_run is None:
        today_run = recent.filter(local_date=today).first()
    return {
        "available": enabled_on_server(),
        "saved": is_saved(autopilot),
        "plan": plan_block(website.user),
        "tracked": tracked_block(website),
        "settings": settings_block(autopilot),
        "engines": engines_block(),
        "google": google_block(),
        "sms": sms_block(autopilot, website, user),
        "allowance": allowance_payload(state),
        "cost": cost_block(autopilot, website, state),
        "next_run_at": iso(next_run_at(autopilot, website)),
        "today_run": pipeline.run_summary(today_run) if today_run is not None else None,
        "runs": [pipeline.run_summary(run) for run in runs],
    }


# -- save -----------------------------------------------------------------------------


def _own_verified(user, subscription_id):
    """The caller's own verified number with this id, or None."""
    from apps.notifications.models import SmsSubscription

    if not subscription_id:
        return None
    try:
        return SmsSubscription.objects.filter(
            id=subscription_id, user=user, status=SmsSubscription.Status.VERIFIED,
        ).first()
    except (ValueError, TypeError, ValidationError):
        return None


def _parse(data: dict) -> dict:
    """The PUT's fields that are present, validated (400 ``invalid``)."""
    out = {}
    for name in BOOL_FIELDS:
        if name in data:
            if not isinstance(data[name], bool):
                raise invalid(name, f"{name} must be true or false.")
            out[name] = data[name]
    for name in TIME_FIELDS:
        if name in data:
            value = parse_time(data[name])
            if value is None:
                raise invalid(name, QUARTER_HOUR)
            out[name] = value
    if "timezone" in data:
        if not known_zone(data["timezone"]):
            raise invalid("timezone", TIMEZONE_MESSAGE)
        out["timezone"] = data["timezone"]
    if "google_cadence" in data:
        if data["google_cadence"] not in CADENCES:
            raise invalid("google_cadence", CADENCE_MESSAGE)
        out["google_cadence"] = data["google_cadence"]
    if "sms_subscription_id" in data:
        raw = data["sms_subscription_id"]
        if raw is not None and not isinstance(raw, str):
            raise invalid("sms_subscription_id", RECIPIENT_MESSAGE)
        if raw:
            try:
                raw = str(uuid.UUID(raw))
            except ValueError:
                raise invalid("sms_subscription_id", RECIPIENT_MESSAGE) from None
        out["sms_subscription_id"] = raw or None
    if "sms_consent" in data:
        if not isinstance(data["sms_consent"], bool):
            raise invalid("sms_consent", CONSENT_MESSAGE)
        out["sms_consent"] = data["sms_consent"]
    return out


def save(website, user, data, ip: str | None = None) -> Autopilot:
    """Save a PUT (contract 3a). Only the fields sent change. Turning the
    text on, or changing its number while it is on, needs the caller's own
    verified number and their agreement, which is stored as a new consent
    record; the opt-in text (template F) then goes after the commit unless
    it went to the same number in the last 24 hours."""
    if not isinstance(data, dict):
        raise invalid("body", BODY_MESSAGE)
    values = _parse(data)
    consent = values.pop("sms_consent", False)
    try:
        return _save(website, user, dict(values), consent, ip)
    except IntegrityError:
        # Two first saves raced on the project's one row: apply this one to
        # the row the other created.
        return _save(website, user, dict(values), consent, ip)


def _save(website, user, values: dict, consent: bool, ip: str | None) -> Autopilot:
    from apps.notifications.services import sms_service

    send_optin = False
    with transaction.atomic():
        autopilot = (
            Autopilot.objects.select_for_update().filter(website=website).first()
            or build_default(website)
        )
        was_enabled, was_on = autopilot.enabled, autopilot.sms_digest
        old_recipient = autopilot.sms_subscription_id
        new_recipient = old_recipient
        if "sms_subscription_id" in values:
            new_recipient = values.pop("sms_subscription_id")
        for name, value in values.items():
            setattr(autopilot, name, value)

        if autopilot.quiet_start == autopilot.quiet_end:
            raise invalid("quiet_end", QUIET_MESSAGE)
        if autopilot.enabled and not (
            autopilot.run_engines or autopilot.run_google or autopilot.run_analysis
        ):
            raise invalid("enabled", NOTHING_ON)
        if autopilot.enabled and not was_enabled and not plan_allows(website.user, "autopilot"):
            raise plan_required()

        changed = str(new_recipient or "") != str(old_recipient or "")
        number = None
        if changed and new_recipient is not None:
            number = _own_verified(user, new_recipient)
            if number is None:
                raise invalid("sms_subscription_id", RECIPIENT_MESSAGE)
        turning_on = autopilot.sms_digest and not was_on
        if turning_on or (autopilot.sms_digest and changed):
            if not plan_allows(website.user, "sms_digest"):
                raise plan_required()
            if not sms_service.is_enabled():
                from apps.notifications.api.v1.sms_views import UNAVAILABLE_MESSAGE

                raise CanseeException(UNAVAILABLE_MESSAGE, code="sms_unavailable", status_code=503)
            number = number or _own_verified(user, new_recipient)
            if number is None:
                raise invalid("sms_subscription_id", RECIPIENT_MESSAGE)
            if not consent:
                raise invalid("sms_consent", CONSENT_MESSAGE)
            store = True
        else:
            # The number's owner agreeing again (a number verified anew
            # needs it); anyone else's agreement changes nothing.
            number = number or (
                _own_verified(user, new_recipient) if autopilot.sms_digest and consent else None
            )
            store = bool(autopilot.sms_digest and consent and number is not None)

        if changed:
            autopilot.sms_subscription_id = new_recipient
            autopilot.sms_optin_sent_at = None
        if store:
            now = timezone.now()
            autopilot.sms_subscription = number
            autopilot.sms_consented_at = now
            autopilot.sms_consent_ip = ip or None
            autopilot.sms_consent_version = Autopilot.CONSENT_VERSION
            recent = (
                autopilot.sms_optin_sent_at is not None
                and now - autopilot.sms_optin_sent_at < OPTIN_INTERVAL
            )
            send_optin = not recent
        autopilot.updated_by = user if getattr(user, "is_authenticated", False) else None
        if autopilot.enabled and autopilot.paused_reason and not was_enabled:
            # Turning it back on after a pause starts the failure count again.
            autopilot.paused_reason = ""
            autopilot.consecutive_failures = 0
        autopilot.save()
        if send_optin:
            autopilot_id = autopilot.id
            transaction.on_commit(lambda: send_optin_text(autopilot_id))
    return autopilot


def send_optin_text(autopilot_id) -> bool:
    """Template F to the project's number, at most once in 24 hours."""
    from apps.autopilot.services import text
    from apps.notifications.services import sms_service

    autopilot = (
        Autopilot.objects.select_related("website", "sms_subscription")
        .filter(id=autopilot_id).first()
    )
    if autopilot is None or not autopilot.sms_digest or autopilot.sms_subscription is None:
        return False
    now = timezone.now()
    if autopilot.sms_optin_sent_at is not None and now - autopilot.sms_optin_sent_at < OPTIN_INTERVAL:
        return False
    body = text.optin_text(autopilot.website)
    try:
        sent = sms_service.notify(autopilot.sms_subscription, body)
    except Exception:
        logger.exception("autopilot: opt-in text for project %s failed", autopilot.website_id)
        return False
    logger.info("autopilot: opt-in text for project %s sent=%s", autopilot.website_id, sent)
    if sent:
        Autopilot.objects.filter(id=autopilot.id).update(sms_optin_sent_at=now)
    return sent
