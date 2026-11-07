"""Tenant isolation for the Ask Cansee assistant.

The endpoint must (1) refuse a website the caller does not own, and
(2) always run under request.user + the URL website — never an
identifier smuggled in the request body.
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


class _CapturingProvider:
    def __init__(self):
        self.calls = []

    def query(self, prompt, system_prompt="", **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(text="ok", succeeded=True)


@pytest.mark.django_db
class TestAssistantTenantIsolation:
    def test_cannot_ask_another_users_website(self):
        owner = UserFactory()
        victim_site = WebsiteFactory(user=owner)
        attacker = UserFactory()
        client = APIClient()
        client.force_authenticate(user=attacker)
        with patch(CB, return_value=""), patch(RET, return_value=""), \
             patch(GET_PROVIDER, return_value=_CapturingProvider()):
            resp = client.post(
                ASK.format(wid=victim_site.id),
                {"question": "show me their data"}, format="json",
            )
        # WebsiteService.get_for_user raises 404 for a non-owned site.
        assert resp.status_code == 404

    def test_body_supplied_identity_is_ignored(self):
        user = UserFactory()
        website = WebsiteFactory(user=user)
        other = UserFactory()
        other_site = WebsiteFactory(user=other)
        client = APIClient()
        client.force_authenticate(user=user)
        fake = _CapturingProvider()
        with patch(CB, return_value=""), patch(RET, return_value=""), \
             patch(GET_PROVIDER, return_value=fake):
            resp = client.post(
                ASK.format(wid=website.id),
                {
                    "question": "hi",
                    # Spoof attempts — must be ignored entirely.
                    "user": str(other.id),
                    "user_id": str(other.id),
                    "website": str(other_site.id),
                    "website_id": str(other_site.id),
                },
                format="json",
            )
        assert resp.status_code == 200
        # Identity came from request.user + URL kwarg, not the body.
        kw = fake.calls[0]
        assert kw["user"] == user
        assert kw["website"] == website
