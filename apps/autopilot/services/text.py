"""The daily text (contract 5, backend 4d).

``build_text`` writes one run's text: templates A to E, every field cleaned
to GSM-7 (``sms_service.gsm7_text``), at most MAX_SEPTETS septets (two
segments). It never relies on ``send_sms`` cutting a long body: past the
budget it shrinks both quoted prompts one character at a time down to 12,
then drops the (+k at 0) note, the run size, the claims, the Google
segment, Weakest, Top and last the link. ``Cansee daily, {P}`` and
``Reply STOP to end`` are never cut. A body still too long raises
``TextTooLong``, which is logged and never sent.

``prepare_text`` decides a finished run's text state, ``deliver_text``
sends a waiting text once it is due: only to the project's verified,
consenting number, outside quiet hours, once (a conditional claim), and
retried once only when Twilio answered 429 or 5xx. Logs carry the run id,
status, length and segments, never the body or the number.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.cache import cache
from django.db.models import F
from django.utils import timezone

from apps.autopilot.models import (
    CADENCE_WEEKLY,
    Autopilot,
    AutopilotRun,
    AutopilotRunPrompt,
)
from apps.autopilot.services import settings as ap
from apps.notifications.services import sms_service
from core.exceptions import CanseeException

logger = logging.getLogger("apps")

Run = AutopilotRun
Row = AutopilotRunPrompt

MAX_SEPTETS = sms_service.MAX_BODY_CHARS
NAME_CHARS = 24
QUOTE_CHARS = 30
QUOTE_MIN = 12
ELLIPSIS = "..."
TAIL = "Reply STOP to end"
PAUSED = "Autopilot is paused after 3 failed runs."
# What is dropped, in order, while a body is over the budget (contract 5a).
DROP_ORDER = ("note", "size", "claims", "google", "weak", "top", "link")
RETRY_MINUTES = 5
MAX_SEND_ATTEMPTS = 2
TEST_INTERVAL_SECONDS = 600

OPTIN = (
    "Cansee: you'll get 1 text a day about {P} after Autopilot runs. Msg&data rates may "
    "apply. Reply HELP for help, STOP to cancel."
)
TEST = "Cansee test: Autopilot texts for {P} will reach this number. Reply STOP to cancel."
NOT_VERIFIED = "Verify this number first."
TOO_MANY_TESTS = "You can send another test text in a few minutes."
TEST_FAILED = "Couldn't send the test text."


class TextTooLong(Exception):
    """A body still over MAX_SEPTETS after every drop (never sent)."""


# -- fields ------------------------------------------------------------------------


def _clean(value) -> str:
    return " ".join(sms_service.gsm7_text(str(value or "")).split())


def _has_letter(value: str) -> bool:
    return any(c.isalpha() for c in value)


def project_name(website) -> str:
    """{P}: the project name in GSM-7, cut to 24 characters; the site's
    domain when no letter is left (a name in another script)."""
    from apps.search_gap.services.google_rank import site_apex

    name = _clean(website.name)
    if not _has_letter(name):
        name = _clean(site_apex(website.url)) or "your project"
    return name[:NAME_CHARS].rstrip()


def prompt_label(text: str, fallback: str) -> str:
    """A prompt in GSM-7; ``fallback`` ("prompt 2") when no letter is left."""
    cleaned = _clean(text)
    return cleaned if _has_letter(cleaned) else fallback


def quote(text: str, limit: int) -> str:
    """At most ``limit`` characters: a longer prompt keeps its first
    ``limit - 3`` plus "..."."""
    if len(text) <= limit:
        return text
    return text[: limit - len(ELLIPSIS)] + ELLIPSIS


def count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def whole_pct(named: int, answered: int) -> int:
    if not answered:
        return 0
    value = Decimal(named) * 100 / Decimal(answered)
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def change_words(pts) -> str | None:
    """``up 3 pts``, ``down 1 pt``, ``no change`` (under 0.5); None when
    not measured."""
    if pts is None:
        return None
    if abs(pts) < 0.5:
        return "no change"
    whole = int(Decimal(str(abs(pts))).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return f"{'up' if pts > 0 else 'down'} {whole} {'pt' if whole == 1 else 'pts'}"


def usd(amount) -> str:
    """``$1.27``, or ``<$0.01`` for a cost under half a cent."""
    value = Decimal(str(amount or 0))
    cents = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if value > 0 and cents == 0:
        return "<$0.01"
    return f"${cents}"


def link(run) -> str:
    base = (getattr(settings, "FRONTEND_URL", "") or "").rstrip("/")
    return _clean(f"{base}/r/{run.link_code}")


def segments(body: str) -> int:
    return sms_service.segments(body)


# -- assembling ----------------------------------------------------------------------


class Part:
    """One sentence of a text. ``template`` may hold ``{q}`` (a quoted
    prompt, shrunk with the budget) and ``{note}`` (dropped first)."""

    def __init__(self, key: str, template: str, *, quoted: str = "", note: str = ""):
        self.key = key
        self.template = template
        self.quoted = quoted
        self.note = note

    def render(self, limit: int, *, note: bool) -> str:
        if not self.quoted and "{note}" not in self.template:
            # Plain text (a link may hold braces): never formatted.
            return self.template
        return self.template.format(
            q=quote(self.quoted, limit), note=self.note if note else "",
        )


def assemble(head: str, parts: list[Part]) -> str:
    """``head`` and the parts within MAX_SEPTETS (contract 5a)."""
    limit = QUOTE_CHARS
    dropped: set = set()

    def body() -> str:
        words = [head]
        words += [
            p.render(limit, note="note" not in dropped) for p in parts if p.key not in dropped
        ]
        words.append(TAIL)
        return " ".join(w for w in words if w)

    def over() -> bool:
        return sms_service.gsm7_septets(body()) > MAX_SEPTETS

    quoted = any(p.quoted and len(p.quoted) > QUOTE_MIN for p in parts)
    while over() and quoted and limit > QUOTE_MIN:
        limit -= 1
    for key in DROP_ORDER:
        if not over():
            break
        dropped.add(key)
    text = body()
    if sms_service.gsm7_septets(text) > MAX_SEPTETS:
        raise TextTooLong(f"{sms_service.gsm7_septets(text)} septets")
    return text


# -- templates -----------------------------------------------------------------------


def _google_part(run) -> Part | None:
    plan = run.plan or {}
    if not plan.get("google") or run.google_status == "off":
        return None
    if not run.google_measured:
        return Part("google", "Google not checked.")
    label = "Google page 1 (this week):" if plan.get("cadence") == CADENCE_WEEKLY else "Google page 1:"
    return Part("google", f"{label} {run.google_page_one}/{run.google_measured}.")


def _claims_part(run) -> Part | None:
    status = run.analysis_status
    if not run.answers_named or status in ("", "off"):
        return None
    if status == "done":
        k = run.new_contradicted or 0
        return Part("claims", f"Claims: {k} new contradicted." if k else "Claims: none new.")
    if status in ("pending", "running"):
        return Part("claims", "Claims pending.")
    return Part("claims", "Claims not checked.")


def _named(run, *, change: bool) -> Part:
    share = whole_pct(run.answers_named, run.answers_answered)
    words = change_words(run.change_pts) if change else None
    tail = f", {words}." if words else "."
    return Part("named", f"Named in {run.answers_named}/{run.answers_answered} answers ({share}%){tail}")


def _engines_asked(run) -> int:
    keys = set()
    for engines in Row.objects.filter(run=run).values_list("engines", flat=True):
        keys.update(k for k, cell in (engines or {}).items() if (cell or {}).get("asked"))
    return len(keys) or len((run.plan or {}).get("engines") or [])


def _size(run) -> Part:
    n = run.prompts_done
    return Part("size", f"{count(n, 'prompt')}, {count(_engines_asked(run), 'engine')}.")


def _top_and_weakest(run) -> list[Part]:
    """Top and Weakest from the 7-day ranking of the run's prompts."""
    from apps.autopilot.services import ranking

    return top_and_weakest_parts(ranking.rank_prompts(run.website, run=run, with_autopilot=False))


def top_and_weakest_parts(ranking: dict) -> list[Part]:
    """Top (the best ranked prompt) and Weakest (the lowest share among the
    others; ties: more answers first), with ``(+k at 0)`` for the other
    ranked prompts no answer named. One ranked prompt: no Weakest; none:
    the unranked line."""
    ranked = [p for p in ranking["prompts"] if p["ranked"]]
    if not ranked:
        return [Part("top", f"Prompts rank after {ranking['min_answers']} answers.")]
    top = ranked[0]
    parts = [Part(
        "top", 'Top: "{q}" ' + f"{top['named']}/{top['answers']}.",
        quoted=prompt_label(top["text"], f"prompt {top['rank']}"),
    )]
    others = [p for p in ranked if p is not top]
    if not others:
        return parts
    weak = min(others, key=lambda p: (p["share_pct"], -p["answers"]))
    zero = sum(1 for p in others if p is not weak and not p["named"])
    parts.append(Part(
        "weak", 'Weakest: "{q}" ' + f"{weak['named']}/{weak['answers']}" + "{note}.",
        quoted=prompt_label(weak["text"], f"prompt {weak['rank']}"),
        note=f" (+{zero} at 0)" if zero else "",
    ))
    return parts


def _partial_why(run) -> str:
    if run.status == Run.STATUS_INTERRUPTED:
        return "stopped early"
    codes = set(
        Row.objects.filter(run=run, status__in=(Row.STATUS_SKIPPED, Row.STATUS_FAILED))
        .values_list("error_code", flat=True)
    )
    if "reserve_reached" in codes:
        return "allowance reserve reached"
    if "allowance_exceeded" in codes:
        return "allowance used up"
    if "cancelled" in codes or "interrupted" in codes:
        return "stopped early"
    unasked = Row.objects.filter(
        run=run, status=Row.STATUS_SKIPPED, error_code="not_configured",
    ).count()
    k = max(0, run.answers_asked - run.answers_answered) + unasked
    return f"{k} answer failed" if k == 1 else f"{k} answers failed"


def _nothing_why(run) -> str:
    plan = run.plan or {}
    if not run.prompts_planned or plan.get("skip") == "nothing_to_run":
        return "No tracked prompts yet"
    if not plan.get("run_engines", True):
        return "AI engines are off in Autopilot"
    if not plan.get("engines"):
        return "No AI engine is configured"
    if run.status == Run.STATUS_INTERRUPTED:
        return "The run stopped early"
    if not run.answers_asked:
        # Every prompt selects models whose engine has no key.
        return "No AI engine is configured"
    k = run.answers_asked
    return f"No engine answered ({k} error{'' if k == 1 else 's'})"


def _cost(run) -> Part:
    return Part("cost", f"AI {usd(run.ai_cost_usd)}.")


def _first_run_parts(run) -> list[Part]:
    return first_run_parts(list(
        Row.objects.filter(run=run, answers_answered__gt=0)
        .select_related("brand_prompt", "brand_prompt__prompt")
    ))


def first_run_parts(rows: list) -> list[Part]:
    """Template E from the run's own answered rows: Named most (most named,
    then most answered, then the oldest tracked; left out when nothing named
    the brand) and Never named (answered prompts with 0 named)."""
    parts = []
    named = [r for r in rows if r.answers_named]
    if named:
        most = min(named, key=lambda r: (
            -r.answers_named, -r.answers_answered, r.brand_prompt.created_at,
        ))
        parts.append(Part(
            "top", 'Named most: "{q}" ' + f"{most.answers_named}/{most.answers_answered}.",
            quoted=prompt_label(most.brand_prompt.prompt.text, f"prompt {most.position + 1}"),
        ))
    never = sum(1 for r in rows if not r.answers_named)
    parts.append(Part("weak", f"Never named: {count(never, 'prompt')}."))
    return parts


def choose_template(run) -> str:
    from apps.autopilot.services.pipeline import ALLOWANCE_CODES, previous_run

    if run.status == Run.STATUS_SKIPPED and run.error_code in ALLOWANCE_CODES:
        return "D"
    if not run.answers_answered:
        return "C"
    if previous_run(run) is None:
        return "E"
    if run.prompts_done < run.prompts_planned:
        return "B"
    return "A"


def build_text(run) -> tuple[str, str]:
    """(body, template) of a finished run (templates A to E)."""
    template = choose_template(run)
    name = project_name(run.website)
    head = f"Cansee daily, {name}:"
    url = Part("link", link(run))
    if template == "D":
        state = ap.allowance_state(run.website.user)
        cap = ap.usd2(state["cap"])
        resume = f"Runs resume {ap.month_day(state['resets_on'])}."
        if run.error_code == "allowance_exceeded":
            parts = [
                Part("status", "skipped today."),
                Part("why", f"Your ${cap} AI allowance for this month is used up."),
            ]
        else:
            pct = 100 - state["reserve_pct"]
            parts = [
                Part("status", "paused."),
                Part("why", f"{pct}% of your ${cap} AI allowance is used; the rest is kept "
                            "for your own questions."),
            ]
        return assemble(head, [*parts, Part("resume", resume), url]), template
    if template == "C":
        parts = [
            Part("status", "no AI answers today."),
            Part("why", f"{_nothing_why(run)}."),
            _google_part(run),
            _cost(run),
            Part("paused", PAUSED) if (run.plan or {}).get("paused") else None,
            url,
        ]
        return assemble(head, [p for p in parts if p is not None]), template
    if template == "E":
        head = f"Cansee daily, {name}, first run:"
        parts = [
            _size(run), _named(run, change=False), *_first_run_parts(run), _google_part(run),
            _cost(run), url,
        ]
        return assemble(head, [p for p in parts if p is not None]), template
    if template == "B":
        why = _partial_why(run)
        parts = [
            Part("partial", f"partial, {run.prompts_done}/{run.prompts_planned} prompts ({why})."),
            _named(run, change=True), *_top_and_weakest(run), _google_part(run), _cost(run), url,
        ]
        for part in parts:
            if part is not None and part.key == "weak":
                part.note = ""
        return assemble(head, [p for p in parts if p is not None]), template
    parts = [
        _size(run), _named(run, change=True), *_top_and_weakest(run), _google_part(run),
        _claims_part(run), _cost(run), url,
    ]
    return assemble(head, [p for p in parts if p is not None]), template


def optin_text(website) -> str:
    """Template F (one segment)."""
    return OPTIN.format(P=project_name(website))


def trial_text(website) -> str:
    """Template G (one segment)."""
    return TEST.format(P=project_name(website))


# -- state and delivery --------------------------------------------------------------


def _log(run_id, status: str, body: str = "", reason: str = "") -> None:
    logger.info(
        "autopilot: text of run %s %s%s (%d chars, %d segments)",
        run_id, status, f" ({reason})" if reason else "", len(body or ""),
        segments(body) if body else 0,
    )


def prepare_text(run) -> None:
    """Build a finished run's text and decide its state (contract 5c):
    ``waiting`` (due now; delivery applies quiet hours), ``not_requested``,
    or ``skipped`` (a stopped run, or the allowance notice already sent this
    billing period)."""
    if run.text_status != Run.TEXT_NOT_REQUESTED or run.text_body:
        return
    pending = Run.objects.filter(id=run.id, text_status=Run.TEXT_NOT_REQUESTED, text_body="")
    if run.status == Run.STATUS_CANCELLED:
        pending.update(text_status=Run.TEXT_SKIPPED, text_reason="cancelled")
        return
    autopilot = Autopilot.objects.filter(website_id=run.website_id).first()
    requested = bool(
        autopilot is not None
        and autopilot.sms_digest
        and autopilot.sms_subscription_id
        and not (run.plan or {}).get("no_text")
        and ap.plan_allows(run.website.user, "sms_digest")
    )
    try:
        body, template = build_text(run)
    except TextTooLong as exc:
        logger.error("autopilot: text of run %s is too long (%s); not sent", run.id, exc)
        pending.update(text_status=Run.TEXT_FAILED if requested else Run.TEXT_NOT_REQUESTED)
        return
    fields = {"text_body": body, "text_template": template, "text_segments": segments(body)}
    if not requested:
        pending.update(**fields)
        return
    if template == "D":
        period = ap.allowance_state(run.website.user)["period"]
        if autopilot.allowance_notice_period == period.key:
            pending.update(**fields, text_status=Run.TEXT_SKIPPED, text_reason="notice_sent")
            _log(run.id, "skipped", body, "notice_sent")
            return
    pending.update(**fields, text_status=Run.TEXT_WAITING, text_due_at=timezone.now())


def _can_open(user, website) -> bool:
    from apps.websites.services.website_service import WebsiteService
    from core.exceptions import ResourceNotFound

    try:
        WebsiteService.get_for_user(user=user, website_id=website.id, min_role="viewer")
    except ResourceNotFound:
        return False
    return True


def deliver_text(run_id, now=None) -> str:
    """Send a waiting text that is due (claimed ``waiting`` to ``sending``
    in one conditional update, so a retry finds it taken)."""
    now = now or timezone.now()
    claimed = Run.objects.filter(
        id=run_id, text_status=Run.TEXT_WAITING, text_due_at__lte=now,
    ).update(text_status=Run.TEXT_SENDING)
    if not claimed:
        return "not_due"
    run = Run.objects.select_related("website", "website__user").get(id=run_id)
    sending = Run.objects.filter(id=run.id, text_status=Run.TEXT_SENDING)

    def end(status: str, reason: str = "", **extra) -> str:
        sending.update(text_status=status, text_reason=reason, **extra)
        _log(run.id, status, run.text_body, reason)
        return status

    if Run.objects.filter(website_id=run.website_id, created_at__gt=run.created_at).exists():
        return end(Run.TEXT_SKIPPED, "superseded")
    autopilot = (
        Autopilot.objects.select_related("sms_subscription", "sms_subscription__user")
        .filter(website_id=run.website_id).first()
    )
    if autopilot is None or not autopilot.sms_digest:
        return end(Run.TEXT_NOT_REQUESTED)
    if not sms_service.is_enabled():
        return end(Run.TEXT_SKIPPED, "unavailable")
    sub = autopilot.sms_subscription
    if sub is None:
        return end(Run.TEXT_SKIPPED, "not_verified")
    if sub.status == sub.Status.OPTED_OUT:
        return end(Run.TEXT_SKIPPED, "opted_out")
    if not sub.is_active:
        return end(Run.TEXT_SKIPPED, "not_verified")
    if autopilot.sms_consented_at is None or (
        sub.consented_at is not None and autopilot.sms_consented_at < sub.consented_at
    ):
        return end(Run.TEXT_SKIPPED, "consent_needed")
    if not _can_open(sub.user, run.website):
        return end(Run.TEXT_SKIPPED, "no_access")
    if not ap.plan_allows(run.website.user, "sms_digest"):
        return end(Run.TEXT_NOT_REQUESTED)
    if ap.in_quiet_hours(autopilot, now):
        due = ap.quiet_end_after(autopilot, now)
        sending.update(text_status=Run.TEXT_WAITING, text_due_at=due)
        _log(run.id, "waiting", run.text_body, "quiet_hours")
        return Run.TEXT_WAITING

    Run.objects.filter(id=run.id).update(text_attempts=F("text_attempts") + 1)
    attempts = run.text_attempts + 1
    try:
        outcome = sms_service.notify_status(sub, run.text_body)
    except Exception:
        logger.exception("autopilot: text of run %s raised", run.id)
        outcome = "failed"
    if outcome == "sent":
        status = end(Run.TEXT_SENT, text_sent_at=timezone.now())
        if run.text_template == "D":
            period = ap.allowance_state(run.website.user)["period"]
            Autopilot.objects.filter(id=autopilot.id).update(allowance_notice_period=period.key)
        return status
    if outcome == "retryable" and attempts < MAX_SEND_ATTEMPTS:
        sending.update(
            text_status=Run.TEXT_WAITING, text_due_at=now + timedelta(minutes=RETRY_MINUTES),
        )
        _log(run.id, "waiting", run.text_body, "retry")
        return Run.TEXT_WAITING
    return end(Run.TEXT_FAILED)


def send_test(website) -> dict:
    """Template G to the project's number, now (a click is a request); at
    most once per project every 10 minutes."""
    from apps.notifications.api.v1.sms_views import UNAVAILABLE_MESSAGE, _masked_phone

    autopilot = (
        Autopilot.objects.select_related("sms_subscription").filter(website=website).first()
    )
    sub = autopilot.sms_subscription if autopilot is not None else None
    if not sms_service.is_enabled():
        raise CanseeException(UNAVAILABLE_MESSAGE, code="sms_unavailable", status_code=503)
    if sub is None or not sub.is_active:
        raise CanseeException(NOT_VERIFIED, code="not_verified", status_code=400)
    key = f"autopilot:test:{website.id}"
    if not cache.add(key, 1, TEST_INTERVAL_SECONDS):
        raise CanseeException(TOO_MANY_TESTS, code="too_many_tests", status_code=429)
    body = trial_text(website)
    if not sms_service.notify(sub, body):
        cache.delete(key)
        raise CanseeException(TEST_FAILED, code="send_failed", status_code=502)
    logger.info("autopilot: test text for project %s sent (%d chars)", website.id, len(body))
    return {"sent": True, "phone_masked": _masked_phone(sub.phone_e164)}
