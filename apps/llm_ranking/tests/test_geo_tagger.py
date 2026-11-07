"""Unit tests for the GEO domain tagger."""

from unittest.mock import patch

import pytest

from apps.llm_ranking.services.geo_tagger import (
    DOMAIN_RECOMMENDATIONS,
    GEO_CATEGORIES,
    TACTICS,
    _heuristic_category,
    recommendations_for,
    tag_prompts,
)


def test_every_category_has_recommendations():
    for cat in GEO_CATEGORIES:
        recs = recommendations_for(cat)
        assert recs, f"category {cat} produced no recommendations"
        assert recs[0]["rank"] == 1
        assert all(r["id"] in TACTICS for r in recs)


def test_law_gov_leads_with_statistics_addition():
    """Per paper Table 3: Law & Gov's top GEO tactic is Statistics Addition."""
    assert DOMAIN_RECOMMENDATIONS["law_gov"][0] == "statistics_addition"
    assert recommendations_for("law_gov")[0]["id"] == "statistics_addition"


def test_debate_leads_with_authoritative():
    """Per paper Table 3: Debate-style queries respond best to Authoritative tone."""
    assert recommendations_for("debate")[0]["id"] == "authoritative"


def test_heuristic_versus_pattern_picks_debate():
    assert _heuristic_category("Hubspot vs Salesforce for small teams") == "debate"


def test_heuristic_law_pattern():
    assert _heuristic_category("GDPR compliance requirements 2026") == "law_gov"


def test_heuristic_history_pattern():
    assert _heuristic_category("history of the internet") == "history"


def test_heuristic_explanation_pattern():
    assert _heuristic_category("how does retrieval augmented generation work") == "explanation"


def test_heuristic_falls_back_to_facts():
    assert _heuristic_category("zzzz qqqq") == "facts"


@pytest.mark.django_db
def test_tag_prompts_uses_heuristic_when_no_api_key(settings):
    settings.OPENAI_API_KEY = ""
    out = tag_prompts(["best CRM for small teams", "GDPR fines 2024"])
    assert out["best CRM for small teams"]["category"] == "opinion"
    assert out["GDPR fines 2024"]["category"] == "law_gov"
    for entry in out.values():
        assert entry["recommendations"]
        assert entry["category_label"]


@pytest.mark.django_db
def test_tag_prompts_uses_llm_when_available(settings):
    settings.OPENAI_API_KEY = "sk-test"

    def fake_llm(prompts):
        return ["history" for _ in prompts]

    with patch("apps.llm_ranking.services.geo_tagger._llm_tag", side_effect=fake_llm):
        out = tag_prompts(["when was Python created"])

    entry = out["when was Python created"]
    assert entry["category"] == "history"
    assert entry["source"] == "llm"


@pytest.mark.django_db
def test_tag_prompts_caches_repeat_lookups(settings):
    settings.OPENAI_API_KEY = "sk-test"
    calls = {"n": 0}

    def fake_llm(prompts):
        calls["n"] += 1
        return ["facts" for _ in prompts]

    with patch("apps.llm_ranking.services.geo_tagger._llm_tag", side_effect=fake_llm):
        tag_prompts(["how many planets are there"])
        tag_prompts(["how many planets are there"])

    assert calls["n"] == 1
