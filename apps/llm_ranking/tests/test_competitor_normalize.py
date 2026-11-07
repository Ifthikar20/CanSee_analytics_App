"""Tests for competitor name normalization."""
from apps.llm_ranking.services.competitor_normalize import (
    MIN_MENTIONS_FOR_RANKING,
    canonical_name,
)


class TestCanonicalName:
    def test_collapses_case_variants(self):
        assert canonical_name("Mixpanel") == canonical_name("mixpanel")
        assert canonical_name("Mixpanel") == canonical_name("MIXPANEL")

    def test_strips_legal_suffixes(self):
        assert canonical_name("Mixpanel Inc.") == canonical_name("Mixpanel")
        assert canonical_name("Stripe LLC") == canonical_name("Stripe")
        assert canonical_name("Acme Co.") == canonical_name("Acme")

    def test_strips_tld(self):
        assert canonical_name("mixpanel.com") == canonical_name("Mixpanel")
        assert canonical_name("vercel.app") == canonical_name("Vercel")
        assert canonical_name("anthropic.ai") == canonical_name("Anthropic")

    def test_handles_punctuation(self):
        assert canonical_name("HubSpot, Inc.") == canonical_name("HubSpot")
        assert canonical_name("Notion-AI") == "notion ai"

    def test_returns_empty_for_empty_input(self):
        assert canonical_name("") == ""
        assert canonical_name("   ") == ""

    def test_preserves_multi_word_names(self):
        # A multi-word brand should keep its words; only suffix/TLD stripped.
        key = canonical_name("HubSpot CRM")
        assert "hubspot" in key
        assert "crm" in key

    def test_min_mentions_threshold(self):
        assert MIN_MENTIONS_FOR_RANKING >= 1
