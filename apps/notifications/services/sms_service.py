"""SMS channel (Twilio).

The most intrusive channel in the product, so it is the most tightly
gated: nothing sends unless SMS_ENABLED is on, credentials exist, the
number is verified, and the subscription has not opted out. Every one of
those is checked at send time rather than trusted from the caller.

Compliance is not decoration here. US A2P texting requires prior express
consent, an honoured STOP, and a HELP response. Those live in
`handle_keyword` and are checked before anything else an inbound message
might mean -- a person texting STOP must be unsubscribed even if the rest
of the pipeline is broken.

No Twilio SDK dependency: the REST API is one form POST, and the webhook
signature is documented HMAC. Adding a package for that is not worth it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import math
import secrets
import unicodedata

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger("apps")

API_ROOT = "https://api.twilio.com/2010-04-01"
SEND_TIMEOUT = 10.0

# A single segment is 160 GSM-7 characters; past that Twilio bills per
# segment and long texts arrive split and reordered on some carriers.
# Two segments is the practical ceiling for something readable.
MAX_BODY_CHARS = 300

VERIFICATION_TTL_SECONDS = 10 * 60
MAX_VERIFICATION_ATTEMPTS = 5

# Carrier-mandated keywords. Matched case-insensitively on the whole body.
STOP_WORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit"}
START_WORDS = {"start", "unstop", "yes"}
HELP_WORDS = {"help", "info"}

# Where HELP points people (one place, so it is a one-line change).
HELP_CONTACT = "support@cansee.ai"
HELP_REPLY = (
    "Cansee: Autopilot daily texts and alerts, about 1 msg a day per project. Text a "
    "question to get an answer from your account data. Reply STOP to unsubscribe, START "
    f"to resume. Help: {HELP_CONTACT}. Msg&data rates may apply."
)
STOP_REPLY = (
    "You're unsubscribed from all Cansee texts, including daily digests. Reply START to "
    "resume."
)
START_REPLY = "You're resubscribed to Cansee texts. Reply STOP to unsubscribe."

# What a send came to (notify_status): sent; retryable when Twilio answered
# 429 or 5xx; failed for anything else, a timeout or a lost connection
# included (Twilio may have sent it, so it is never sent again).
SENT = "sent"
RETRYABLE = "retryable"
FAILED = "failed"

# -- GSM-7 -----------------------------------------------------------------------
#
# One character outside GSM-7 turns a whole text into UCS-2 (70 characters a
# segment instead of 160), so texts are cleaned to the basic set and its
# extension table (two septets each) before they are measured or sent.

GSM7_BASIC = frozenset(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
GSM7_EXTENSION = frozenset("^{}\\[~]|€\f")
_GSM7_MAP = {
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'", "\u00b4": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"', "\u2033": '"',
    "\u00ab": '"', "\u00bb": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2015": "-",
    "\u2212": "-",
    "\u2026": "...",
}


def gsm7_text(text: str) -> str:
    """``text`` in GSM-7: curly quotes become straight, dashes ``-``, the
    ellipsis ``...``, other spaces a space, accents outside the set are
    stripped (``á`` to ``a``) and anything else (emoji included) is dropped."""
    out = []
    for ch in text or "":
        if ch in GSM7_BASIC or ch in GSM7_EXTENSION:
            out.append(ch)
        elif ch in _GSM7_MAP:
            out.append(_GSM7_MAP[ch])
        elif ch.isspace():
            out.append(" ")
        else:
            out.append("".join(
                c for c in unicodedata.normalize("NFKD", ch)
                if c in GSM7_BASIC and not unicodedata.combining(c)
            ))
    return "".join(out)


def is_gsm7(text: str) -> bool:
    return all(ch in GSM7_BASIC or ch in GSM7_EXTENSION for ch in text or "")


def gsm7_septets(text: str) -> int:
    """Septets a GSM-7 text takes: two for an extension character."""
    return sum(2 if ch in GSM7_EXTENSION else 1 for ch in text or "")


def segments(text: str) -> int:
    """Segments Twilio bills for ``text``: GSM-7 is one up to 160 septets,
    else 153 a segment; anything else is UCS-2 (70, else 67 a segment)."""
    if not text:
        return 0
    if is_gsm7(text):
        septets = gsm7_septets(text)
        return 1 if septets <= 160 else math.ceil(septets / 153)
    units = len(text.encode("utf-16-le")) // 2
    return 1 if units <= 70 else math.ceil(units / 67)


def is_configured() -> bool:
    return bool(
        getattr(settings, "TWILIO_ACCOUNT_SID", "")
        and getattr(settings, "TWILIO_AUTH_TOKEN", "")
        and getattr(settings, "TWILIO_FROM_NUMBER", "")
    )


def is_enabled() -> bool:
    """Deployment switch, checked before credentials so the feature can be
    turned off without pulling secrets."""
    return bool(getattr(settings, "SMS_ENABLED", False)) and is_configured()


def hash_phone(phone_e164: str) -> str:
    """Lookup digest for an encrypted number.

    Keyed with the app secret so the digest is not a plain SHA of a
    guessable value -- the phone-number space is small enough to enumerate
    against an unkeyed hash.
    """
    key = (getattr(settings, "SECRET_KEY", "") or "").encode()
    return hmac.new(key, normalize_phone(phone_e164).encode(), hashlib.sha256).hexdigest()


def normalize_phone(raw: str) -> str:
    """Reduce to E.164-ish: a leading + and digits only.

    Deliberately not a full phone-number parser. Twilio rejects malformed
    numbers at send time with a clear error, and carrying libphonenumber
    for a settings field is not worth the weight.
    """
    text = (raw or "").strip()
    digits = "".join(c for c in text if c.isdigit())
    if not digits:
        return ""
    return f"+{digits}"


def looks_like_phone(raw: str) -> bool:
    digits = "".join(c for c in (raw or "") if c.isdigit())
    # E.164 allows 15 digits max; anything under 8 is not a mobile number.
    return 8 <= len(digits) <= 15


# -- verification --------------------------------------------------------


def generate_code() -> str:
    """A 6-digit code from a CSPRNG."""
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_code(code: str) -> str:
    key = (getattr(settings, "SECRET_KEY", "") or "").encode()
    return hmac.new(key, (code or "").strip().encode(), hashlib.sha256).hexdigest()


def code_matches(subscription, code: str) -> bool:
    """Constant-time compare, with expiry and attempt ceiling enforced."""
    if not subscription.verification_code_hash or not subscription.verification_sent_at:
        return False
    if subscription.verification_attempts >= MAX_VERIFICATION_ATTEMPTS:
        return False
    age = (timezone.now() - subscription.verification_sent_at).total_seconds()
    if age > VERIFICATION_TTL_SECONDS:
        return False
    return hmac.compare_digest(subscription.verification_code_hash, hash_code(code))


# -- outbound ------------------------------------------------------------


def _send(to_e164: str, body: str) -> str:
    """Send one message; SENT, RETRYABLE or FAILED. Never raises: a failed
    alert must not take down the scan or webhook that triggered it. Logs
    never carry the body or the number."""
    if not is_enabled():
        logger.info("sms: skipped, channel disabled or unconfigured")
        return FAILED
    to = normalize_phone(to_e164)
    if not looks_like_phone(to):
        logger.warning("sms: refusing to send to a malformed number")
        return FAILED

    try:
        import requests
    except ImportError:  # pragma: no cover - requests is a hard dependency
        return FAILED

    sid = settings.TWILIO_ACCOUNT_SID
    payload = {
        "To": to,
        "From": settings.TWILIO_FROM_NUMBER,
        "Body": (body or "")[:MAX_BODY_CHARS],
    }
    try:
        resp = requests.post(
            f"{API_ROOT}/Accounts/{sid}/Messages.json",
            data=payload,
            auth=(sid, settings.TWILIO_AUTH_TOKEN),
            timeout=SEND_TIMEOUT,
        )
    except Exception as exc:
        # A timeout or a lost connection: Twilio may have sent it.
        logger.warning("sms: send failed: %s", exc)
        return FAILED

    if resp.status_code == 429 or 500 <= resp.status_code < 600:
        logger.warning("sms: twilio returned HTTP %s (retryable)", resp.status_code)
        return RETRYABLE
    if resp.status_code >= 300:
        # Never log the body: it contains the recipient number.
        logger.warning("sms: twilio returned HTTP %s", resp.status_code)
        return FAILED
    return SENT


def send_sms(to_e164: str, body: str) -> bool:
    """Send one message. Returns False rather than raising."""
    return _send(to_e164, body) == SENT


def notify_status(subscription, body: str) -> str:
    """Send to a subscription, honouring its state: SENT, RETRYABLE (Twilio
    answered 429 or 5xx) or FAILED (anything else, a timeout or a lost
    connection included)."""
    if subscription is None or not subscription.is_active:
        return FAILED
    status = _send(subscription.phone_e164, body)
    if status == SENT:
        subscription.last_message_at = timezone.now()
        subscription.save(update_fields=["last_message_at", "updated_at"])
    return status


def notify(subscription, body: str) -> bool:
    """Send to a subscription, honouring its state. The only path alerts
    should use -- it is what makes an opt-out actually stick."""
    return notify_status(subscription, body) == SENT


# -- inbound -------------------------------------------------------------


def verify_twilio_signature(request) -> bool:
    """Validate X-Twilio-Signature.

    Twilio signs the full URL plus the POST fields sorted by key, HMAC-SHA1
    with the auth token, base64 encoded. Without this the webhook is an
    open endpoint anyone can post to and impersonate a subscriber.
    """
    token = (getattr(settings, "TWILIO_AUTH_TOKEN", "") or "").encode()
    if not token:
        return False
    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature:
        return False

    # Twilio signs the URL it was configured with. Behind nginx/Cloudflare
    # the reconstructed scheme can differ, so allow an explicit override.
    url = getattr(settings, "TWILIO_WEBHOOK_URL", "") or request.build_absolute_uri()
    payload = url + "".join(
        f"{k}{v}" for k, v in sorted(request.POST.items())
    )
    expected = base64.b64encode(
        hmac.new(token, payload.encode("utf-8"), hashlib.sha1).digest()
    ).decode()
    return hmac.compare_digest(expected, signature)


def handle_keyword(subscriptions, body: str) -> str | None:
    """Apply STOP / START / HELP to every subscription of the sending
    number. Returns the reply, or None if the message was not a keyword
    and should be treated as a question.

    STOP opts out every row, for every account, pending ones included.
    START revives only opted-out rows that were verified once
    (``consented_at`` set): a number someone else added but never verified
    stays opted out, so the owner's START cannot make it textable by that
    account.

    Checked before any other interpretation: an opt-out has to work even
    when everything downstream is failing.
    """
    if subscriptions is None:
        rows = []
    elif hasattr(subscriptions, "status"):
        rows = [subscriptions]
    else:
        rows = [s for s in subscriptions if s is not None]
    word = (body or "").strip().lower()
    if word in STOP_WORDS:
        now = timezone.now()
        for subscription in rows:
            if subscription.status == subscription.Status.OPTED_OUT:
                continue
            subscription.status = subscription.Status.OPTED_OUT
            subscription.opted_out_at = now
            subscription.save(update_fields=["status", "opted_out_at", "updated_at"])
        return STOP_REPLY
    if word in START_WORDS:
        for subscription in rows:
            if (
                subscription.status == subscription.Status.OPTED_OUT
                and subscription.consented_at is not None
            ):
                subscription.status = subscription.Status.VERIFIED
                subscription.opted_out_at = None
                subscription.save(update_fields=["status", "opted_out_at", "updated_at"])
        return START_REPLY
    if word in HELP_WORDS:
        return HELP_REPLY
    return None


def twiml(message: str = "") -> str:
    """Minimal TwiML. An empty <Response/> tells Twilio to send nothing."""
    if not message:
        return "<?xml version='1.0' encoding='UTF-8'?><Response/>"
    safe = (
        str(message)[:MAX_BODY_CHARS]
        .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    return f"<?xml version='1.0' encoding='UTF-8'?><Response><Message>{safe}</Message></Response>"
