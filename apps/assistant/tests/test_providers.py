"""Context providers: routing, resilience, and tenant scoping.

The routing tests pin the behaviour the user actually complained about —
"how are my prompts doing?" must load prompt metrics, not just traffic.
"""
import pytest

from apps.accounts.tests.factories import UserFactory
from apps.assistant.services import providers as P
from apps.assistant.services.context_builder import build_fact_block
from apps.prompt_library.models import BrandPrompt
from apps.prompt_library.tests.factories import PromptFactory
from apps.websites.tests.factories import WebsiteFactory


class TestRouting:
    def test_prompt_question_selects_prompt_areas(self):
        keys = {p.key for p in P.select_providers("how are my prompts doing?")}
        assert "prompt_metrics" in keys or "prompts" in keys

    def test_search_question_selects_search_console(self):
        keys = {p.key for p in P.select_providers(
            "which keywords bring me clicks in google search console?")}
        assert "search_insights" in keys

    def test_security_question_selects_security(self):
        keys = {p.key for p in P.select_providers("any hallucination findings?")}
        assert "security" in keys

    def test_citation_question_selects_citations(self):
        keys = {p.key for p in P.select_providers("which domains get cited most?")}
        assert "citations" in keys

    def test_vague_question_falls_back_to_defaults(self):
        keys = {p.key for p in P.select_providers("how am I doing?")}
        assert keys == {p.key for p in P.PROVIDERS if p.default}
        assert "traffic" in keys

    def test_selection_is_capped(self):
        # A keyword-stuffed question must not pull every subsystem.
        q = ("traffic visibility prompt search console citation audit content "
             "agent usage knowledge security")
        assert len(P.select_providers(q)) <= 5


@pytest.mark.django_db
class TestProvidersAreScopedAndSafe:
    def test_every_provider_runs_clean_on_an_empty_account(self):
        """No provider may raise on a brand-new website with no data."""
        user = UserFactory()
        website = WebsiteFactory(user=user)
        for provider in P.PROVIDERS:
            lines = provider.fn(user, website)
            assert isinstance(lines, list)
            assert all(isinstance(x, str) for x in lines)

    def test_prompts_provider_only_sees_own_website(self):
        a_user = UserFactory()
        a_site = WebsiteFactory(user=a_user)
        b_user = UserFactory()
        b_site = WebsiteFactory(user=b_user)
        BrandPrompt.objects.create(
            website=b_site, prompt=PromptFactory(text="competitor secret prompt"),
        )
        lines = "\n".join(P.prompts_lines(a_user, a_site))
        assert "competitor secret prompt" not in lines

    def test_build_sections_survives_a_broken_provider(self, monkeypatch):
        user = UserFactory()
        website = WebsiteFactory(user=user)

        def boom(*_a, **_k):
            raise RuntimeError("provider exploded")

        monkeypatch.setattr(P, "traffic_lines", boom)
        broken = P.ContextProvider("traffic", "TRAFFIC", boom, default=True)
        monkeypatch.setattr(P, "PROVIDERS", (broken,) + P.PROVIDERS[1:])
        # Must not raise; the failed section is simply absent.
        sections = P.build_sections(user, website, "how is my traffic?")
        assert all(label != "TRAFFIC" for label, _ in sections)


@pytest.mark.django_db
class TestContextBlock:
    def test_question_pulls_prompt_section(self):
        user = UserFactory()
        website = WebsiteFactory(user=user)
        BrandPrompt.objects.create(
            website=website, prompt=PromptFactory(text="best payment platform"),
        )
        block = build_fact_block(user, website, question="how are my prompts doing?")
        assert "best payment platform" in block or "PROMPT" in block

    def test_block_is_bounded(self):
        user = UserFactory()
        website = WebsiteFactory(user=user)
        for i in range(60):
            BrandPrompt.objects.create(
                website=website, prompt=PromptFactory(text=f"prompt number {i} " + "x" * 200),
            )
        block = build_fact_block(user, website, question="show me my prompts")
        assert len(block) <= 9000
