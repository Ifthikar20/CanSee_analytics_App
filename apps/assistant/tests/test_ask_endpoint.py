"""Ask Cansee endpoint contract + tenant gate.

Hermetic: the provider and RAG retrieval are patched so no LLM/embedding
network call happens.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from apps.accounts.tests.factories import UserFactory
from apps.websites.tests.factories import WebsiteFactory

ASK = "/api/v1/assistant/{wid}/ask/"

CB = "apps.assistant.services.context_builder.build_fact_block"
RET = "apps.rag.services.retriever.retrieve_context_block"
GET_PROVIDER = "apps.llm_ranking.providers.get_provider"
GET_SYNTH = "apps.llm_ranking.providers.get_synthesis_provider"


class _FakeProvider:
    """Records the query() call and returns a fixed answer."""
    def __init__(self, text="**Here** is your answer."):
        self.text = text
        self.calls = []

    def query(self, prompt, system_prompt="", **kwargs):
        self.calls.append({"prompt": prompt, "system": system_prompt, "kwargs": kwargs})
        return SimpleNamespace(text=self.text, succeeded=True)


@pytest.fixture
def auth():
    user = UserFactory()
    website = WebsiteFactory(user=user)
    client = APIClient()
    client.force_authenticate(user=user)
    return client, user, website


@pytest.mark.django_db
class TestAskEndpoint:
    def test_requires_auth(self):
        wid = WebsiteFactory().id
        resp = APIClient().post(ASK.format(wid=wid), {"question": "hi"}, format="json")
        assert resp.status_code in (401, 403)

    def test_happy_path(self, auth):
        client, user, website = auth
        fake = _FakeProvider("**All good.**")
        with patch(CB, return_value="LIVE FACTS"), \
             patch(RET, return_value=""), \
             patch(GET_PROVIDER, return_value=fake):
            resp = client.post(
                ASK.format(wid=website.id),
                {"question": "How is my traffic today?"}, format="json",
            )
        assert resp.status_code == 200
        body = resp.json()["data"]  # global success/data envelope
        assert body["answer"] == "**All good.**"
        assert body["grounded"] is True
        # The provider was called under THIS user + website, module=assistant.
        kw = fake.calls[0]["kwargs"]
        assert kw["user"] == user
        assert kw["website"] == website
        assert kw["module"] == "assistant"

    def test_missing_question_is_400(self, auth):
        client, _u, website = auth
        resp = client.post(ASK.format(wid=website.id), {}, format="json")
        assert resp.status_code == 400

    def test_provider_unavailable(self, auth):
        client, _u, website = auth
        with patch(CB, return_value=""), patch(RET, return_value=""), \
             patch(GET_PROVIDER, return_value=None), \
             patch(GET_SYNTH, return_value=None):
            resp = client.post(
                ASK.format(wid=website.id), {"question": "hi"}, format="json",
            )
        assert resp.status_code == 200
        assert "No AI provider" in resp.json()["data"]["answer"]

    def test_rate_limited(self, auth):
        client, _u, website = auth
        deny = SimpleNamespace(try_acquire=lambda: False)
        with patch("apps.assistant.api.v1.views._ask_bucket", return_value=deny):
            resp = client.post(
                ASK.format(wid=website.id), {"question": "hi"}, format="json",
            )
        assert resp.status_code == 429

    def test_history_is_accepted(self, auth):
        client, _u, website = auth
        fake = _FakeProvider()
        with patch(CB, return_value=""), patch(RET, return_value=""), \
             patch(GET_PROVIDER, return_value=fake):
            resp = client.post(
                ASK.format(wid=website.id),
                {"question": "and yesterday?",
                 "history": [
                     {"role": "user", "content": "How is my traffic today?"},
                     {"role": "assistant", "content": "1,240 visits."},
                 ]},
                format="json",
            )
        assert resp.status_code == 200
        assert "Conversation so far" in fake.calls[0]["prompt"]
