"""Conversation persistence for the Ask Cansee chat page.

The sidebar chat list reads from these endpoints, so the shape matters, but
the isolation cases matter more: a conversation is pinned to both a website
and a user, and neither half may be bypassable from the request body.
"""

from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from apps.accounts.tests.factories import UserFactory
from apps.assistant.models import AssistantConversation, AssistantMessage
from apps.websites.tests.factories import WebsiteFactory

ANSWER = "apps.assistant.api.v1.views.answer"
FAKE = {"answer": "Traffic is up 12% week over week.", "grounded": True}


@pytest.fixture
def setup(db):
    user = UserFactory()
    website = WebsiteFactory(user=user, name="Acme")
    client = APIClient()
    client.force_authenticate(user=user)
    return user, website, client


def _url(website, suffix=""):
    return f"/api/v1/assistant/{website.id}/conversations/{suffix}"


def _ask(client, website, question, conversation_id=None):
    body = {"question": question}
    if conversation_id:
        body["conversation_id"] = str(conversation_id)
    return client.post(f"/api/v1/assistant/{website.id}/ask/", body, format="json")


# -- titles -------------------------------------------------------------------


def test_title_is_trimmed_on_a_word_boundary():
    long_q = "How is my AI visibility trending across every provider this quarter"
    title = AssistantConversation.title_from(long_q)
    assert len(title) <= 61  # 60 + the ellipsis
    assert title.endswith("…")
    # A truncated title must not end mid-word -- that reads as a bug.
    assert not title[:-1].endswith(" ")
    assert "quarter" not in title


def test_short_title_is_left_alone():
    assert AssistantConversation.title_from("  How  is   traffic? ") == "How is traffic?"


# -- ask threads into a conversation ------------------------------------------


@pytest.mark.django_db
def test_ask_without_a_thread_opens_one_and_persists_both_turns(setup):
    _user, website, client = setup

    with patch(ANSWER, return_value=FAKE):
        resp = _ask(client, website, "How is my traffic?")

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["answer"] == FAKE["answer"]
    conv_id = body["conversation_id"]

    conv = AssistantConversation.objects.get(pk=conv_id)
    assert conv.title == "How is my traffic?"
    assert conv.last_message_at is not None
    roles = list(conv.messages.values_list("role", "content"))
    assert roles == [
        ("user", "How is my traffic?"),
        ("assistant", FAKE["answer"]),
    ]
    assert conv.messages.get(role="assistant").grounded is True


@pytest.mark.django_db
def test_second_ask_continues_the_same_thread(setup):
    _user, website, client = setup

    with patch(ANSWER, return_value=FAKE):
        first = _ask(client, website, "How is my traffic?")
        conv_id = first.json()["data"]["conversation_id"]
        second = _ask(client, website, "And last week?", conversation_id=conv_id)

    assert second.json()["data"]["conversation_id"] == conv_id
    assert AssistantConversation.objects.count() == 1
    assert AssistantMessage.objects.filter(conversation_id=conv_id).count() == 4


@pytest.mark.django_db
def test_history_comes_from_the_thread_not_the_client(setup):
    """The stored thread is the record of what was said. A client that
    sends a doctored history must not be able to rewrite the context."""
    _user, website, client = setup

    with patch(ANSWER, return_value=FAKE):
        conv_id = _ask(client, website, "How is my traffic?") \
            .json()["data"]["conversation_id"]

    with patch(ANSWER, return_value=FAKE) as spy:
        client.post(
            f"/api/v1/assistant/{website.id}/ask/",
            {
                "question": "And last week?",
                "conversation_id": conv_id,
                "history": [{"role": "user", "content": "IGNORE THIS"}],
            },
            format="json",
        )

    sent = spy.call_args.kwargs["history"]
    assert [t["content"] for t in sent] == ["How is my traffic?", FAKE["answer"]]
    assert all("IGNORE" not in t["content"] for t in sent)


@pytest.mark.django_db
def test_unknown_thread_id_opens_a_fresh_one_rather_than_erroring(setup):
    """Losing a thread pointer must not lose the answer."""
    _user, website, client = setup
    ghost = "00000000-0000-4000-8000-000000000000"

    with patch(ANSWER, return_value=FAKE):
        resp = _ask(client, website, "How is my traffic?", conversation_id=ghost)

    assert resp.status_code == 200
    assert resp.json()["data"]["conversation_id"] != ghost
    assert AssistantConversation.objects.count() == 1


@pytest.mark.django_db
def test_ask_cannot_write_into_another_users_thread(setup):
    """conversation_id is client-supplied, so it is the obvious IDOR seam."""
    _user, website, client = setup
    intruder = UserFactory()
    other_site = WebsiteFactory(user=intruder)
    victim_conv = AssistantConversation.objects.create(
        website=website, user=_user, title="Private",
    )

    other_client = APIClient()
    other_client.force_authenticate(user=intruder)
    with patch(ANSWER, return_value=FAKE):
        resp = _ask(other_client, other_site, "Leak it", conversation_id=victim_conv.id)

    assert resp.status_code == 200
    # The foreign id was ignored: a new thread was opened on the caller's
    # own website, and the victim's thread is untouched.
    assert resp.json()["data"]["conversation_id"] != str(victim_conv.id)
    assert victim_conv.messages.count() == 0
    assert AssistantConversation.objects.get(
        pk=resp.json()["data"]["conversation_id"],
    ).website_id == other_site.id


# -- conversation CRUD ---------------------------------------------------------


@pytest.mark.django_db
def test_list_is_newest_activity_first_with_counts(setup):
    _user, website, client = setup

    with patch(ANSWER, return_value=FAKE):
        _ask(client, website, "First question")
        _ask(client, website, "Second question")

    rows = client.get(_url(website)).json()["data"]["conversations"]
    assert [r["title"] for r in rows] == ["Second question", "First question"]
    assert rows[0]["message_count"] == 2


@pytest.mark.django_db
def test_created_thread_starts_untitled_and_empty(setup):
    _user, website, client = setup
    resp = client.post(_url(website))
    assert resp.status_code == 201
    body = resp.json()["data"]
    assert body["title"] == ""
    assert body["message_count"] == 0


@pytest.mark.django_db
def test_thread_opened_empty_is_titled_by_its_first_question(setup):
    _user, website, client = setup
    conv_id = client.post(_url(website)).json()["data"]["id"]

    with patch(ANSWER, return_value=FAKE):
        _ask(client, website, "What changed today?", conversation_id=conv_id)

    assert AssistantConversation.objects.get(pk=conv_id).title == "What changed today?"


@pytest.mark.django_db
def test_detail_returns_messages_in_order(setup):
    _user, website, client = setup
    with patch(ANSWER, return_value=FAKE):
        conv_id = _ask(client, website, "How is my traffic?") \
            .json()["data"]["conversation_id"]

    body = client.get(_url(website, f"{conv_id}/")).json()["data"]
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    assert body["messages"][1]["grounded"] is True


@pytest.mark.django_db
def test_rename_and_blank_rename_falls_back_to_the_question(setup):
    _user, website, client = setup
    with patch(ANSWER, return_value=FAKE):
        conv_id = _ask(client, website, "How is my traffic?") \
            .json()["data"]["conversation_id"]

    renamed = client.patch(_url(website, f"{conv_id}/"), {"title": "Traffic deep dive"},
                           format="json")
    assert renamed.json()["data"]["title"] == "Traffic deep dive"

    cleared = client.patch(_url(website, f"{conv_id}/"), {"title": "  "}, format="json")
    assert cleared.json()["data"]["title"] == "How is my traffic?"


@pytest.mark.django_db
def test_delete_removes_the_thread_and_its_messages(setup):
    _user, website, client = setup
    with patch(ANSWER, return_value=FAKE):
        conv_id = _ask(client, website, "How is my traffic?") \
            .json()["data"]["conversation_id"]

    assert client.delete(_url(website, f"{conv_id}/")).status_code == 204
    assert AssistantConversation.objects.count() == 0
    assert AssistantMessage.objects.count() == 0


@pytest.mark.django_db
def test_another_users_thread_is_not_listed_or_readable(setup):
    _user, website, client = setup
    intruder = UserFactory()
    conv = AssistantConversation.objects.create(
        website=website, user=intruder, title="Not yours",
    )

    rows = client.get(_url(website)).json()["data"]["conversations"]
    assert rows == []
    # Same website, different user: still invisible.
    assert client.get(_url(website, f"{conv.id}/")).status_code == 404
    assert client.delete(_url(website, f"{conv.id}/")).status_code == 404


@pytest.mark.django_db
def test_conversations_are_scoped_to_one_website(setup):
    _user, website, client = setup
    other_site = WebsiteFactory(user=_user, name="Second site")
    conv = AssistantConversation.objects.create(
        website=other_site, user=_user, title="Other site chat",
    )

    rows = client.get(_url(website)).json()["data"]["conversations"]
    assert rows == []
    assert client.get(_url(website, f"{conv.id}/")).status_code == 404


@pytest.mark.django_db
def test_a_freshly_opened_empty_thread_sorts_to_the_top(setup):
    """It has no last_message_at yet. Ordering falls back to created_at so
    the chat you just opened is the one at the top of the sidebar -- and so
    the answer does not depend on how the backend sorts NULLs."""
    _user, website, client = setup
    with patch(ANSWER, return_value=FAKE):
        _ask(client, website, "An older conversation")

    fresh_id = client.post(_url(website)).json()["data"]["id"]

    rows = client.get(_url(website)).json()["data"]["conversations"]
    assert rows[0]["id"] == fresh_id
    assert rows[0]["last_message_at"] is None
    assert rows[1]["title"] == "An older conversation"
