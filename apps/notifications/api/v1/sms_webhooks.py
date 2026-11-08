"""Inbound SMS webhook (Twilio).

Two-way texting: a subscriber can reply to an alert with a plain-English
question and get an answer from the same assistant that backs the Ask
page. When the assistant cannot answer, the reply says so.

Order of business on every inbound message:

  1. Verify the Twilio signature. Without it this is an open endpoint and
     anyone can impersonate a subscriber's number.
  2. Apply STOP / START / HELP. Carrier-mandated, and an opt-out has to
     work even if everything after it is broken.
  3. Otherwise answer the question, scoped to that subscriber's own data.

Tenancy: the answering user comes from the SUBSCRIPTION matched on the
sending number, never from the message body. A text cannot ask about
someone else's account because there is no field in which to name one.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.http import HttpResponse, HttpResponseForbidden
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.notifications.services import sms_service

logger = logging.getLogger("apps")

# A text answer has to fit a couple of segments, so the assistant is asked
# for something far shorter than the web UI would render.
SMS_ANSWER_CHARS = 280

NOT_REGISTERED = (
    "This number isn't linked to a Cansee account. Add it in Settings to "
    "get alerts and ask questions here."
)
NO_WEBSITE = (
    "No project is set up on your account yet, so there's nothing to "
    "report on."
)
REPLIES_OFF = (
    "Replies are turned off for this number. You'll still get alerts. "
    "Reply STOP to unsubscribe."
)
AI_UNAVAILABLE = "Cansee couldn't answer right now: AI isn't available."
VIEW_ONLY = (
    "Your organization role can view this project but not ask questions "
    "about it by text. Ask an organization admin for member access."
)


def _xml(body: str) -> HttpResponse:
    return HttpResponse(body, content_type="application/xml")


def _find_subscriptions(from_number: str) -> list:
    """Every subscription of the sender's number (every account that added
    it), newest first, matched by keyed digest.

    The stored number is encrypted and therefore unqueryable, which is why
    the hash column exists. Opted-out and pending rows are included on
    purpose: STOP has to reach every row of the number, and a START from a
    lapsed subscriber has to find its rows to resurrect them.
    """
    from apps.notifications.models import SmsSubscription

    if not sms_service.normalize_phone(from_number):
        return []
    digest = sms_service.hash_phone(from_number)
    if not digest:
        return []
    return list(
        SmsSubscription.objects
        .select_related("user")
        .filter(phone_hash=digest)
        .order_by("-created_at")
    )


def _answer_for(subscription, question: str) -> str:
    """Ask the assistant, scoped to this subscriber's first website."""
    from apps.assistant.services.orchestrator import AssistantUnavailable, answer
    from apps.websites.services.website_service import WebsiteService

    user = subscription.user
    # Answering spends engine runs, which the app allows members and above
    # (a viewer gets a 403 there), so the role is re-read here too.
    website = (
        WebsiteService.created_by_qs_with_role(user, "member")
        .order_by("created_at").first()
    )
    if website is None:
        if WebsiteService.created_by_qs(user).exists():
            return VIEW_ONLY
        return NO_WEBSITE

    try:
        result = answer(
            user=user,
            website=website,
            question=(
                f"{question}\n\n"
                "Answer in under 280 characters of plain text. No Markdown, no "
                "tables, no bullet lists — this is being read as a text message."
            ),
        )
    except AssistantUnavailable as exc:
        if exc.code == AssistantUnavailable.NOT_CONFIGURED:
            return AI_UNAVAILABLE
        return exc.message[:SMS_ANSWER_CHARS]
    return (result.get("answer") or "").strip()[:SMS_ANSWER_CHARS]


@csrf_exempt
@require_POST
def twilio_inbound(request):
    """POST /api/v1/notifications/sms/inbound/ — Twilio message webhook."""
    if not getattr(settings, "SMS_ENABLED", False):
        # Silently accept and do nothing: Twilio retries on non-2xx, and a
        # disabled feature should not generate a retry storm.
        return _xml(sms_service.twiml())

    if not sms_service.verify_twilio_signature(request):
        logger.warning("sms: inbound rejected, bad or missing signature")
        return HttpResponseForbidden("invalid signature")

    from_number = request.POST.get("From", "")
    body = (request.POST.get("Body", "") or "").strip()
    subscriptions = _find_subscriptions(from_number)

    # Keywords first, always, and for every row of the number.
    keyword_reply = sms_service.handle_keyword(subscriptions, body)
    if keyword_reply is not None:
        return _xml(sms_service.twiml(keyword_reply))

    if not subscriptions:
        return _xml(sms_service.twiml(NOT_REGISTERED))
    # A question is answered from the newest verified row's account.
    subscription = next((s for s in subscriptions if s.is_active), None)
    if subscription is None:
        # Pending or opted out: no answers, and no nagging either. HELP and
        # START still work above, which is the only escape hatch needed.
        return _xml(sms_service.twiml())
    if not subscription.allow_replies:
        return _xml(sms_service.twiml(REPLIES_OFF))
    if not body:
        return _xml(sms_service.twiml(sms_service.HELP_REPLY))

    try:
        reply = _answer_for(subscription, body)
    except Exception:
        logger.exception("sms: answering an inbound question failed")
        reply = "Something went wrong answering that. Please try again shortly."
    return _xml(sms_service.twiml(reply))
