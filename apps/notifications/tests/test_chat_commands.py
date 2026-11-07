"""Tests for the answer_chat_command router and its reply delivery."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.brand_vault.models import SafetyAlert
from apps.notifications.models import IntegrationConnection
from apps.notifications.tasks import answer_chat_command
from apps.websites.tests.factories import WebsiteFactory

SLACK_RESPONSE_URL = {"kind": "slack_response_url", "url": "https://hooks.slack.com/commands/T1/1/a"}
DISCORD_FOLLOWUP = {"kind": "discord_followup", "interaction_token": "tok", "interaction_id": "123"}


def _slack_connection(user=None):
    return IntegrationConnection.objects.create(
        user=user or UserFactory(),
        platform="slack",
        webhook_url="https://hooks.slack.com/services/T0/B0/xyz",
        external_team_id="T123",
    )


def _discord_connection(user=None):
    return IntegrationConnection.objects.create(
        user=user or UserFactory(),
        platform="discord",
        webhook_url="https://discord.com/api/webhooks/1/abc",
        external_team_id="G123",
    )


def _slack_reply_text(mock_post) -> str:
    assert mock_post.called
    return mock_post.call_args.kwargs["json"]["text"]


def _claude_stub(succeeded=True, text="narrated summary"):
    """(patched class, instance) pair standing in for core.llm.ClaudeUtility.

    Report/digest tests must always stub narration: the developer's shell may
    export a real ANTHROPIC_API_KEY, and an unstubbed ClaudeUtility would
    make a live API call.
    """
    instance = MagicMock()
    instance.query.return_value = SimpleNamespace(succeeded=succeeded, text=text)
    return MagicMock(return_value=instance), instance


@pytest.mark.django_db
class TestCommandRouter:
    def test_help_lists_all_commands(self):
        connection = _slack_connection()
        with patch("apps.notifications.tasks.requests.post") as mock_post:
            answer_chat_command(
                connection_id=str(connection.id), command="help",
                text="", respond_to=SLACK_RESPONSE_URL,
            )
        text = _slack_reply_text(mock_post)
        for command in ("report", "growth", "security", "usage", "ask", "scan"):
            assert command in text

    def test_unknown_command_falls_back_to_help(self):
        connection = _slack_connection()
        with patch("apps.notifications.tasks.requests.post") as mock_post:
            answer_chat_command(
                connection_id=str(connection.id), command="bogus",
                text="", respond_to=SLACK_RESPONSE_URL,
            )
        assert "Cansee commands" in _slack_reply_text(mock_post)

    def test_unknown_connection_is_a_noop(self):
        with patch("apps.notifications.tasks.requests.post") as mock_post:
            answer_chat_command(
                connection_id="00000000-0000-0000-0000-000000000000",
                command="help", text="", respond_to=SLACK_RESPONSE_URL,
            )
        mock_post.assert_not_called()

    def test_report_is_honest_when_empty(self):
        connection = _slack_connection()
        with patch("core.llm.ClaudeUtility", _claude_stub(succeeded=False)[0]), \
             patch("apps.notifications.tasks.requests.post") as mock_post:
            answer_chat_command(
                connection_id=str(connection.id), command="report",
                text="", respond_to=SLACK_RESPONSE_URL,
            )
        text = _slack_reply_text(mock_post)
        assert "Daily Growth Report" in text
        assert "No AI visibility data yet" in text
        assert "No open brand-security alerts" in text

    def test_report_over_discord_uses_embed_followup(self, settings):
        settings.DISCORD_APPLICATION_ID = "app-1"
        connection = _discord_connection()
        with patch("core.llm.ClaudeUtility", _claude_stub(succeeded=False)[0]), patch(
            "apps.notifications.services.discord_service.DiscordService.send_followup"
        ) as mock_followup:
            answer_chat_command(
                connection_id=str(connection.id), command="report",
                text="", respond_to=DISCORD_FOLLOWUP,
            )
        kwargs = mock_followup.call_args.kwargs
        assert kwargs["application_id"] == "app-1"
        assert kwargs["interaction_token"] == "tok"
        assert "Daily Growth Report" in kwargs["title"]

    def test_security_empty_points_at_scan(self):
        connection = _slack_connection()
        with patch("apps.notifications.tasks.requests.post") as mock_post:
            answer_chat_command(
                connection_id=str(connection.id), command="security",
                text="", respond_to=SLACK_RESPONSE_URL,
            )
        text = _slack_reply_text(mock_post)
        assert "No open brand-security alerts" in text
        assert "brand-security scan" in text

    def test_security_lists_alerts_by_severity_with_mitigation(self):
        connection = _slack_connection()
        website = WebsiteFactory(user=connection.user)
        SafetyAlert.objects.create(
            website=website, issue=SafetyAlert.ISSUE_WEAK_ENDORSEMENT,
            severity=SafetyAlert.SEVERITY_LOW, snippet="meh",
            title="Weak endorsement of brand",
        )
        SafetyAlert.objects.create(
            website=website, issue=SafetyAlert.ISSUE_HALLUCINATION,
            severity=SafetyAlert.SEVERITY_HIGH, snippet="wrong facts",
            title="Wrong pricing quoted",
        )
        # Resolved alerts must not count.
        SafetyAlert.objects.create(
            website=website, issue=SafetyAlert.ISSUE_HARMFUL,
            severity=SafetyAlert.SEVERITY_HIGH, snippet="old",
            status=SafetyAlert.STATUS_RESOLVED,
        )
        with patch("apps.notifications.tasks.requests.post") as mock_post:
            answer_chat_command(
                connection_id=str(connection.id), command="security",
                text="", respond_to=SLACK_RESPONSE_URL,
            )
        text = _slack_reply_text(mock_post)
        assert "2 open alerts" in text
        assert "(1 high, 0 medium, 1 low)" in text
        # High severity listed before low.
        assert text.index("Wrong pricing quoted") < text.index("Weak endorsement of brand")
        # Mitigation comes from the detector registry (ISSUE_FALLBACK ->
        # BS-FACT-001.recommended_action for hallucination rows).
        assert "Correct the record" in text

    def test_ask_without_agent_uses_one_shot_provider(self):
        connection = _slack_connection()
        WebsiteFactory(user=connection.user)
        provider = MagicMock()
        provider.query.return_value = SimpleNamespace(text="synth answer")
        with patch(
            "apps.llm_ranking.providers.get_provider", return_value=provider,
        ), patch(
            "apps.rag.services.retriever.retrieve_context_block", return_value="",
        ), patch("apps.notifications.tasks.requests.post") as mock_post:
            answer_chat_command(
                connection_id=str(connection.id), command="ask",
                text="what changed", respond_to=SLACK_RESPONSE_URL,
            )
        assert _slack_reply_text(mock_post) == "synth answer"
        kwargs = provider.query.call_args.kwargs
        assert kwargs["module"] == "notifications"
        assert kwargs["role"] == "chat_command"

    def test_ask_without_provider_is_honest(self):
        connection = _slack_connection()
        WebsiteFactory(user=connection.user)
        with patch(
            "apps.llm_ranking.providers.get_provider", return_value=None,
        ), patch(
            "apps.llm_ranking.providers.get_synthesis_provider", return_value=None,
        ), patch("apps.notifications.tasks.requests.post") as mock_post:
            answer_chat_command(
                connection_id=str(connection.id), command="ask",
                text="anything", respond_to=SLACK_RESPONSE_URL,
            )
        assert "No AI provider is configured" in _slack_reply_text(mock_post)

    def test_ask_without_website_is_honest(self):
        connection = _slack_connection()
        with patch("apps.notifications.tasks.requests.post") as mock_post:
            answer_chat_command(
                connection_id=str(connection.id), command="ask",
                text="anything", respond_to=SLACK_RESPONSE_URL,
            )
        assert "No active website" in _slack_reply_text(mock_post)

    def test_scan_without_prompts_is_honest(self):
        connection = _slack_connection()
        WebsiteFactory(user=connection.user)
        from apps.llm_ranking.models import LLMRankingAudit
        with patch("apps.notifications.tasks.requests.post") as mock_post:
            answer_chat_command(
                connection_id=str(connection.id), command="scan",
                text="", respond_to=SLACK_RESPONSE_URL,
            )
        assert "no saved prompts" in _slack_reply_text(mock_post)
        assert LLMRankingAudit.objects.count() == 0

    def test_scan_queues_audit_from_saved_prompts(self):
        connection = _slack_connection()
        website = WebsiteFactory(user=connection.user)
        from apps.llm_ranking.models import LLMRankingAudit
        prompts = [{"text": "best analytics tools?", "type": "generic"}]
        with patch(
            "apps.llm_ranking.services.audit_runner.gather_saved_prompts",
            return_value=prompts,
        ), patch(
            "apps.llm_ranking.services.scan_dispatch.dispatch_scan",
        ) as mock_dispatch, patch("apps.notifications.tasks.requests.post") as mock_post:
            answer_chat_command(
                connection_id=str(connection.id), command="scan",
                text="", respond_to=SLACK_RESPONSE_URL,
            )
        audit = LLMRankingAudit.objects.get(website=website)
        assert audit.prompts == prompts
        mock_dispatch.assert_called_once_with(str(audit.id))
        assert "queued" in _slack_reply_text(mock_post)


@pytest.mark.django_db
class TestReportNarration:
    def test_report_reply_is_claude_narrated(self, settings):
        settings.FRONTEND_URL = "https://app.example.test"
        connection = _slack_connection()
        utility, instance = _claude_stub(text="Growth held steady today; run a scan next.")
        with patch("core.llm.ClaudeUtility", utility), \
             patch("apps.notifications.tasks.requests.post") as mock_post:
            answer_chat_command(
                connection_id=str(connection.id), command="report",
                text="", respond_to=SLACK_RESPONSE_URL,
            )
        text = _slack_reply_text(mock_post)
        assert "Growth held steady today; run a scan next." in text
        assert "https://app.example.test" in text
        assert "cansee.ai" not in text
        # Narrator wiring: cheap model, bounded tokens, attributed call.
        assert utility.call_args.kwargs == {"model": "claude-haiku-4-5", "max_tokens": 500}
        kwargs = instance.query.call_args.kwargs
        assert kwargs["role"] == "chat_report_narration"
        assert kwargs["module"] == "notifications"
        assert kwargs["user"] == connection.user
        # The prompt is the deterministic fact serialization.
        facts = instance.query.call_args.args[0]
        assert "Traffic (last 24h)" in facts
        assert "no completed visibility scans yet" in facts

    def test_report_falls_back_to_template_when_narration_fails(self, settings):
        settings.FRONTEND_URL = "https://app.example.test"
        connection = _slack_connection()
        with patch("core.llm.ClaudeUtility", _claude_stub(succeeded=False)[0]), \
             patch("apps.notifications.tasks.requests.post") as mock_post:
            answer_chat_command(
                connection_id=str(connection.id), command="report",
                text="", respond_to=SLACK_RESPONSE_URL,
            )
        text = _slack_reply_text(mock_post)
        assert "Traffic:" in text
        assert "No AI visibility data yet" in text
        assert "View the full dashboard at https://app.example.test" in text
        assert "cansee.ai" not in text

    def test_report_survives_narration_exception(self):
        connection = _slack_connection()
        with patch("core.llm.ClaudeUtility", MagicMock(side_effect=RuntimeError("boom"))), \
             patch("apps.notifications.tasks.requests.post") as mock_post:
            answer_chat_command(
                connection_id=str(connection.id), command="report",
                text="", respond_to=SLACK_RESPONSE_URL,
            )
        assert "Traffic:" in _slack_reply_text(mock_post)


@pytest.mark.django_db
class TestDailyDigestNarration:
    def test_detailed_slack_digest_uses_claude_narration(self):
        from apps.notifications.tasks import send_daily_growth_reports

        connection = _slack_connection()
        connection.message_format = "detailed"
        connection.frequency = "daily"
        connection.save(update_fields=["message_format", "frequency"])
        utility, instance = _claude_stub(text="Your day in growth, told plainly.")
        with patch("core.llm.ClaudeUtility", utility), patch(
            "apps.notifications.services.slack_service.SlackService.send_message"
        ) as mock_send:
            send_daily_growth_reports()
        assert instance.query.called
        blocks = mock_send.call_args.kwargs["blocks"]
        section_texts = [
            b["text"]["text"] for b in blocks if b.get("type") == "section"
        ]
        assert any("Your day in growth, told plainly." in t for t in section_texts)
        # Narrated layout replaces the field-by-field template.
        assert not any("*Traffic*" in t for t in section_texts)

    def test_summary_digest_keeps_template_and_skips_llm(self):
        from apps.notifications.tasks import send_daily_growth_reports

        connection = _slack_connection()
        connection.frequency = "daily"  # message_format stays "summary"
        connection.save(update_fields=["frequency"])
        utility, _ = _claude_stub()
        with patch("core.llm.ClaudeUtility", utility), patch(
            "apps.notifications.services.slack_service.SlackService.send_message"
        ) as mock_send:
            send_daily_growth_reports()
        utility.assert_not_called()
        blocks = mock_send.call_args.kwargs["blocks"]
        section_texts = [
            b["text"]["text"] for b in blocks if b.get("type") == "section"
        ]
        assert any("*Traffic*" in t for t in section_texts)

    def test_discord_digest_footer_uses_frontend_url(self, settings):
        from apps.notifications.tasks import send_daily_growth_reports

        settings.FRONTEND_URL = "https://app.example.test"
        connection = _discord_connection()
        connection.frequency = "daily"
        connection.save(update_fields=["frequency"])
        with patch(
            "apps.notifications.services.discord_service.DiscordService.send_message"
        ) as mock_send:
            send_daily_growth_reports()
        kwargs = mock_send.call_args.kwargs
        assert kwargs["footer"] == "View the full dashboard at https://app.example.test"
        assert "cansee.ai" not in str(kwargs)


@pytest.mark.django_db
class TestUsageCommand:
    def test_usage_empty_is_honest(self):
        connection = _slack_connection()
        with patch("apps.notifications.tasks.requests.post") as mock_post:
            answer_chat_command(
                connection_id=str(connection.id), command="usage",
                text="", respond_to=SLACK_RESPONSE_URL,
            )
        text = _slack_reply_text(mock_post)
        assert "AI Usage" in text
        assert "No AI usage recorded this billing period" in text
        assert "scan" in text

    def test_usage_reports_tokens_allowance_and_top_modules(self):
        usage = {
            "by_module": [
                {"module": "rag", "cost": 0.05, "tokens": 400, "calls": 1},
                {"module": "llm_ranking", "cost": 0.10, "tokens": 1000, "calls": 1},
                {"module": "onboarding", "cost": 0.01, "tokens": 50, "calls": 1},
            ],
            "totals": {"total_tokens": 1400, "calls": 2},
            "allowance": {
                "used_tokens": 1400,
                "cap_usd": 29.25,
                "spent_usd": 0.15,
                "pct_used": 0.5,
                "resets_at": "2026-09-01T00:00:00+00:00",
            },
        }
        connection = _slack_connection()
        with patch(
            "apps.metering.services.usage_reader.get_period_usage",
            return_value=usage,
        ), patch("apps.notifications.tasks.requests.post") as mock_post:
            answer_chat_command(
                connection_id=str(connection.id), command="usage",
                text="", respond_to=SLACK_RESPONSE_URL,
            )
        text = _slack_reply_text(mock_post)
        assert "1,400 tokens across 2 AI requests" in text
        assert "$0.15 of $29.25 used (0.5%)" in text
        assert "Resets on Sep 01" in text
        # Top 2 modules by spend, highest first; the third is dropped.
        assert "llm_ranking ($0.10), rag ($0.05)" in text
        assert "onboarding" not in text


@pytest.mark.django_db
class TestGrowthCommand:
    def test_growth_empty_is_honest(self):
        connection = _slack_connection()
        WebsiteFactory(user=connection.user)
        with patch("apps.notifications.tasks.requests.post") as mock_post:
            answer_chat_command(
                connection_id=str(connection.id), command="growth",
                text="", respond_to=SLACK_RESPONSE_URL,
            )
        text = _slack_reply_text(mock_post)
        assert "No completed prompt runs yet" in text
        assert "scan" in text

    def test_growth_lists_movers_and_focus(self):
        connection = _slack_connection()
        WebsiteFactory(user=connection.user, name="Alpha")
        WebsiteFactory(user=connection.user, name="Beta")
        site_overviews = [
            {"has_data": True, "brand_current": 42.0, "brand_delta_pct": 12.5,
             "competitor_current": 30.0, "competitor_delta_pct": 2.0},
            {"has_data": True, "brand_current": 18.0, "brand_delta_pct": -4.2,
             "competitor_current": 22.0, "competitor_delta_pct": 1.0},
        ]
        prompts_overview = {
            "has_data": True,
            "prompts": [  # weakest-first, as the real builder returns them
                {"text": "cheap crm tools", "visibility": 0.0},
                {"text": "best crm for smb", "visibility": 25.0},
                {"text": "crm with ai", "visibility": 50.0},
                {"text": "top rated crm", "visibility": 90.0},
            ],
        }
        with patch(
            "apps.llm_ranking.services.visibility_series.build_overview_for_website",
            side_effect=site_overviews,
        ), patch(
            "apps.llm_ranking.services.overview_stats.build_overview_for_user",
            return_value=prompts_overview,
        ), patch("apps.notifications.tasks.requests.post") as mock_post:
            answer_chat_command(
                connection_id=str(connection.id), command="growth",
                text="", respond_to=SLACK_RESPONSE_URL,
            )
        text = _slack_reply_text(mock_post)
        assert "Growth Movers" in text
        assert "Moving up:" in text
        assert "Alpha: 42.0% AI visibility (+12.5% trend" in text
        assert "Slipping:" in text
        assert "Beta: 18.0% AI visibility (-4.2% trend" in text
        assert "Strongest prompts:" in text
        assert '"top rated crm" - 90.0% visibility' in text
        assert "Needs work:" in text
        assert '"cheap crm tools" - 0.0% visibility' in text
        assert 'Suggested focus: improve coverage for "cheap crm tools"' in text


@pytest.mark.django_db
class TestAskFactPack:
    def test_ask_prompt_includes_yesterday_comparison_traffic(self):
        connection = _slack_connection()
        WebsiteFactory(user=connection.user, name="Alpha")
        provider = MagicMock()
        provider.query.return_value = SimpleNamespace(text="grounded answer")
        overviews = [
            {"total_visitors": 120, "total_pageviews": 300},  # last 24h
            {"total_visitors": 100, "total_pageviews": 260},  # previous 24h
        ]
        with patch(
            "apps.llm_ranking.providers.get_provider", return_value=provider,
        ), patch(
            "apps.rag.services.retriever.retrieve_context_block", return_value="",
        ), patch(
            "apps.analytics.services.analytics_service.AnalyticsService.get_overview",
            side_effect=overviews,
        ), patch("apps.notifications.tasks.requests.post") as mock_post:
            answer_chat_command(
                connection_id=str(connection.id), command="ask",
                text="how much has the traffic grown since yesterday",
                respond_to=SLACK_RESPONSE_URL,
            )
        prompt = provider.query.call_args.args[0]
        assert "Live account facts as of" in prompt
        assert "Traffic for Alpha: last 24h 120 visitors / 300 pageviews" in prompt
        assert "previous 24h (yesterday) 100 visitors / 260 pageviews" in prompt
        assert "visitor change +20.0% vs yesterday" in prompt
        assert "never fabricate numbers" in prompt
        assert _slack_reply_text(mock_post) == "grounded answer"

    def test_ask_prompt_includes_saved_prompts_and_latest_audit_content(self):
        from django.utils import timezone

        from apps.citations.models import Citation
        from apps.llm_ranking.models import LLMRankingAudit
        from apps.llm_ranking.tests.factories import (
            LLMRankingAuditFactory,
            LLMRankingResultFactory,
        )
        from apps.prompt_library.models import BrandPrompt
        from apps.prompt_library.tests.factories import PromptFactory

        connection = _slack_connection()
        website = WebsiteFactory(user=connection.user, name="Alpha")

        oldest = PromptFactory(text="What are the best CRM tools for startups?")
        BrandPrompt.objects.create(website=website, prompt=oldest, tags=["branded"])
        BrandPrompt.objects.create(
            website=website, prompt=PromptFactory(text="Best growth dashboards?"),
        )
        newest = PromptFactory(text="Which analytics platform suits a small SaaS team?")
        BrandPrompt.objects.create(website=website, prompt=newest)

        audit = LLMRankingAuditFactory(
            website=website, created_by=connection.user,
            status=LLMRankingAudit.STATUS_COMPLETED, completed_at=timezone.now(),
        )
        mentioned = LLMRankingResultFactory(
            audit=audit, prompt="best crm for smb", prompt_index=0,
            is_mentioned=True, mention_rank=1,
            competitors_mentioned=[{"name": "Rival CRM", "position": 2}],
        )
        LLMRankingResultFactory(
            audit=audit, prompt="cheap crm tools", prompt_index=1,
            is_mentioned=False, mention_rank=None,
        )
        Citation.objects.create(
            result=mentioned, audit=audit,
            url="https://www.reddit.com/r/crm/comments/1",
            normalized_url="reddit.com/r/crm/comments/1",
            domain="www.reddit.com", apex_domain="reddit.com",
            source_class="reddit", reference_count=1,
        )

        provider = MagicMock()
        provider.query.return_value = SimpleNamespace(text="grounded answer")
        with patch(
            "apps.llm_ranking.providers.get_provider", return_value=provider,
        ), patch(
            "apps.rag.services.retriever.retrieve_context_block", return_value="",
        ), patch(
            "apps.analytics.services.analytics_service.AnalyticsService.get_overview",
            side_effect=Exception("analytics offline"),
        ), patch("apps.notifications.tasks.requests.post") as mock_post:
            answer_chat_command(
                connection_id=str(connection.id), command="ask",
                text="what about my most recent prompt",
                respond_to=SLACK_RESPONSE_URL,
            )
        prompt = provider.query.call_args.args[0]
        # Saved prompts: newest first, indexed, with the active flag and tags.
        assert "Saved prompts, most recent first:" in prompt
        assert '1. "Which analytics platform suits a small SaaS team?" (active)' in prompt
        assert prompt.index("Which analytics platform suits a small SaaS team?") < \
            prompt.index("What are the best CRM tools for startups?")
        assert '"What are the best CRM tools for startups?" (active) [tags: branded]' in prompt
        # Latest completed audit content: per-prompt visibility, competitor
        # brands, and cited domains, all from real rows.
        assert "Latest completed visibility audit" in prompt
        assert '- "cheap crm tools" - 0.0% visibility' in prompt
        assert "- Rival CRM: 50.0% visibility" in prompt
        assert "- reddit.com: 100.0% of retrievals" in prompt
        # The system prompt defines what "most recent prompt" means.
        system_prompt = provider.query.call_args.args[1]
        assert "'my most recent prompt' means the first entry" in system_prompt
        assert _slack_reply_text(mock_post) == "grounded answer"

    def test_ask_fact_block_capped_at_4000_chars_content_truncated(self):
        from apps.notifications.tasks import (
            ASK_FACT_BLOCK_CHAR_CAP,
            ASK_FACT_BLOCK_MORE_LINE,
            _live_fact_block,
        )

        user = UserFactory()
        website = WebsiteFactory(user=user)
        oversized = ["Saved prompts, most recent first:"] + [
            f'{i}. "{"prompt text " * 8}" (active)' for i in range(1, 201)
        ]
        with patch(
            "apps.notifications.tasks._saved_prompt_fact_lines",
            return_value=oversized,
        ), patch(
            "apps.analytics.services.analytics_service.AnalyticsService.get_overview",
            side_effect=Exception("analytics offline"),
        ):
            block = _live_fact_block(user, website)
        assert len(block) <= ASK_FACT_BLOCK_CHAR_CAP
        # Numbers section leads and survives; the content section is cut and
        # announces the cut instead of ending mid-list.
        assert block.startswith("Live account facts as of")
        assert block.endswith(ASK_FACT_BLOCK_MORE_LINE)
        assert "Saved prompts, most recent first:" in block
        assert block.index("Brand security") < block.index("Saved prompts")
