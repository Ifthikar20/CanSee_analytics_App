"""SMS channel: compliance, tenancy and the two-way reply path.

The compliance cases are the ones that matter most here. STOP has to work
under every condition, an unverified or opted-out number must never be
sent to, and the inbound webhook must reject anything it cannot prove came
from Twilio.
"""

import base64
import hashlib
import hmac
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.accounts.tests.factories import UserFactory
from apps.notifications.models import SmsSubscription
from apps.notifications.services import sms_service
from apps.websites.tests.factories import WebsiteFactory

PHONE = "+14155550123"
INBOUND_URL = "/api/v1/notifications/sms/inbound/"


@pytest.fixture
def sms_on(settings):
    settings.SMS_ENABLED = True
    settings.TWILIO_ACCOUNT_SID = "AC-test"
    settings.TWILIO_AUTH_TOKEN = "tok-test"
    settings.TWILIO_FROM_NUMBER = "+15005550006"
    settings.TWILIO_WEBHOOK_URL = f"http://testserver{INBOUND_URL}"
    return settings


def _sub(user, **kw):
    defaults = {
        "phone_e164": PHONE,
        "phone_hash": sms_service.hash_phone(PHONE),
        "status": SmsSubscription.Status.VERIFIED,
        "consented_at": timezone.now(),
    }
    defaults.update(kw)
    return SmsSubscription.objects.create(user=user, **defaults)


def _sign(url, params, token="tok-test"):
    payload = url + "".join(f"{k}{v}" for k, v in sorted(params.items()))
    return base64.b64encode(
        hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()
    ).decode()


def _post(client, params, *, sign=True, token="tok-test"):
    url = f"http://testserver{INBOUND_URL}"
    headers = {}
    if sign:
        headers["HTTP_X_TWILIO_SIGNATURE"] = _sign(url, params, token)
    return client.post(INBOUND_URL, params, **headers)


# -- phone handling -----------------------------------------------------------


def test_normalize_and_validate_numbers():
    assert sms_service.normalize_phone(" (415) 555-0123 ") == "+4155550123"
    assert sms_service.normalize_phone("") == ""
    assert sms_service.looks_like_phone(PHONE)
    assert not sms_service.looks_like_phone("123")
    assert not sms_service.looks_like_phone("")


def test_phone_hash_is_keyed_and_stable(settings):
    """The digest must not be a bare SHA of the number: the phone-number
    space is small enough to enumerate one."""
    settings.SECRET_KEY = "key-a"
    a = sms_service.hash_phone(PHONE)
    # Same number, different formatting -> same digest (normalised first).
    assert a == sms_service.hash_phone(" 1 (415) 555-0123 ")
    # A different number (no country code) must NOT collide.
    assert a != sms_service.hash_phone("+4155550123")
    assert a != hashlib.sha256(PHONE.encode()).hexdigest()
    settings.SECRET_KEY = "key-b"
    assert sms_service.hash_phone(PHONE) != a


# -- verification codes -------------------------------------------------------


@pytest.mark.django_db
def test_verification_code_expires_and_is_attempt_limited(settings):
    user = UserFactory()
    code = sms_service.generate_code()
    assert len(code) == 6 and code.isdigit()

    sub = _sub(
        user,
        status=SmsSubscription.Status.PENDING,
        verification_code_hash=sms_service.hash_code(code),
        verification_sent_at=timezone.now(),
    )
    assert sms_service.code_matches(sub, code)
    assert not sms_service.code_matches(sub, "000000")

    # Too many guesses.
    sub.verification_attempts = sms_service.MAX_VERIFICATION_ATTEMPTS
    assert not sms_service.code_matches(sub, code)

    # Expired.
    sub.verification_attempts = 0
    sub.verification_sent_at = timezone.now() - timezone.timedelta(
        seconds=sms_service.VERIFICATION_TTL_SECONDS + 60,
    )
    assert not sms_service.code_matches(sub, code)


# -- sending gates ------------------------------------------------------------


@pytest.mark.django_db
def test_notify_refuses_unverified_and_opted_out(sms_on):
    user = UserFactory()
    with patch.object(sms_service, "send_sms", return_value=True) as send:
        assert sms_service.notify(_sub(user, status=SmsSubscription.Status.PENDING), "hi") is False
        SmsSubscription.objects.all().delete()
        assert sms_service.notify(_sub(user, status=SmsSubscription.Status.OPTED_OUT), "hi") is False
        assert sms_service.notify(None, "hi") is False
    send.assert_not_called()


@pytest.mark.django_db
def test_notify_sends_to_a_verified_number(sms_on):
    user = UserFactory()
    sub = _sub(user)
    with patch.object(sms_service, "send_sms", return_value=True):
        assert sms_service.notify(sub, "Visibility dropped 9 points.") is True
    sub.refresh_from_db()
    assert sub.last_message_at is not None


def test_send_is_a_no_op_when_the_channel_is_off(settings):
    settings.SMS_ENABLED = False
    with patch("requests.post") as post:
        assert sms_service.send_sms(PHONE, "hi") is False
    post.assert_not_called()


def test_send_refuses_a_malformed_number(sms_on):
    with patch("requests.post") as post:
        assert sms_service.send_sms("12", "hi") is False
    post.assert_not_called()


# -- compliance keywords ------------------------------------------------------


@pytest.mark.django_db
def test_stop_opts_out_and_start_resubscribes():
    user = UserFactory()
    sub = _sub(user)

    assert sms_service.handle_keyword(sub, "STOP") == sms_service.STOP_REPLY
    sub.refresh_from_db()
    assert sub.status == SmsSubscription.Status.OPTED_OUT
    assert sub.opted_out_at is not None
    assert sub.is_active is False

    assert sms_service.handle_keyword(sub, " start ") == sms_service.START_REPLY
    sub.refresh_from_db()
    assert sub.status == SmsSubscription.Status.VERIFIED
    assert sub.opted_out_at is None


@pytest.mark.django_db
def test_every_carrier_stop_word_is_honoured():
    user = UserFactory()
    for word in sms_service.STOP_WORDS:
        SmsSubscription.objects.all().delete()
        sub = _sub(user)
        sms_service.handle_keyword(sub, word.upper())
        sub.refresh_from_db()
        assert sub.status == SmsSubscription.Status.OPTED_OUT, word


def test_help_replies_and_a_question_is_not_a_keyword():
    assert sms_service.handle_keyword(None, "HELP") == sms_service.HELP_REPLY
    assert sms_service.handle_keyword(None, "how is my traffic?") is None


def test_stop_works_even_with_no_subscription():
    """An unknown number must still get an opt-out confirmation."""
    assert sms_service.handle_keyword(None, "STOP") == sms_service.STOP_REPLY


# -- inbound webhook ----------------------------------------------------------


@pytest.mark.django_db
def test_inbound_rejects_an_unsigned_request(client, sms_on):
    resp = _post(client, {"From": PHONE, "Body": "hello"}, sign=False)
    assert resp.status_code == 403


@pytest.mark.django_db
def test_inbound_rejects_a_forged_signature(client, sms_on):
    resp = _post(client, {"From": PHONE, "Body": "hello"}, token="wrong-token")
    assert resp.status_code == 403


@pytest.mark.django_db
def test_inbound_stop_unsubscribes(client, sms_on):
    user = UserFactory()
    sub = _sub(user)
    resp = _post(client, {"From": PHONE, "Body": "STOP"})
    assert resp.status_code == 200
    assert "unsubscribed" in resp.content.decode().lower()
    sub.refresh_from_db()
    assert sub.status == SmsSubscription.Status.OPTED_OUT


@pytest.mark.django_db
def test_inbound_unknown_number_is_told_it_is_not_linked(client, sms_on):
    resp = _post(client, {"From": "+19995550000", "Body": "how is my traffic?"})
    assert resp.status_code == 200
    assert "isn't linked" in resp.content.decode()


@pytest.mark.django_db
def test_inbound_question_is_answered_for_the_matched_user(client, sms_on):
    """Tenancy: the answering user comes from the number, never the body."""
    user = UserFactory()
    website = WebsiteFactory(user=user)
    _sub(user)

    with patch(
        "apps.assistant.services.orchestrator.answer",
        return_value={"answer": "Visibility is 6.3%.", "grounded": True},
    ) as ans:
        resp = _post(client, {"From": PHONE, "Body": "how is my visibility?"})

    assert resp.status_code == 200
    assert "Visibility is 6.3%." in resp.content.decode()
    assert ans.call_args.kwargs["user"] == user
    assert ans.call_args.kwargs["website"].id == website.id


@pytest.mark.django_db
def test_inbound_answer_is_truncated_for_a_text(client, sms_on):
    user = UserFactory()
    WebsiteFactory(user=user)
    _sub(user)
    with patch(
        "apps.assistant.services.orchestrator.answer",
        return_value={"answer": "x" * 4000, "grounded": True},
    ):
        resp = _post(client, {"From": PHONE, "Body": "tell me everything"})
    # TwiML wrapper plus a body capped well under a runaway multi-segment send.
    assert len(resp.content) < 1200


@pytest.mark.django_db
def test_inbound_opted_out_number_gets_no_answer(client, sms_on):
    user = UserFactory()
    WebsiteFactory(user=user)
    _sub(user, status=SmsSubscription.Status.OPTED_OUT)
    with patch("apps.assistant.services.orchestrator.answer") as ans:
        resp = _post(client, {"From": PHONE, "Body": "how is my traffic?"})
    assert resp.status_code == 200
    ans.assert_not_called()
    assert "<Response/>" in resp.content.decode()


@pytest.mark.django_db
def test_inbound_respects_replies_disabled(client, sms_on):
    user = UserFactory()
    WebsiteFactory(user=user)
    _sub(user, allow_replies=False)
    with patch("apps.assistant.services.orchestrator.answer") as ans:
        resp = _post(client, {"From": PHONE, "Body": "how is my traffic?"})
    ans.assert_not_called()
    assert "Replies are turned off" in resp.content.decode()


@pytest.mark.django_db
def test_inbound_survives_an_assistant_failure(client, sms_on):
    user = UserFactory()
    WebsiteFactory(user=user)
    _sub(user)
    with patch(
        "apps.assistant.services.orchestrator.answer",
        side_effect=RuntimeError("model down"),
    ):
        resp = _post(client, {"From": PHONE, "Body": "how is my traffic?"})
    assert resp.status_code == 200
    assert "went wrong" in resp.content.decode()


@pytest.mark.django_db
def test_inbound_is_inert_when_the_channel_is_off(client, settings):
    settings.SMS_ENABLED = False
    resp = client.post(INBOUND_URL, {"From": PHONE, "Body": "STOP"})
    # 200 with an empty response: a non-2xx would make Twilio retry.
    assert resp.status_code == 200
    assert "<Response/>" in resp.content.decode()


# -- TwiML --------------------------------------------------------------------


def test_twiml_escapes_and_empty_response():
    assert sms_service.twiml() == "<?xml version='1.0' encoding='UTF-8'?><Response/>"
    out = sms_service.twiml('5 < 10 & "up"')
    assert "&lt;" in out and "&amp;" in out
    assert "<Message>" in out
