"""Extraction v4: the kind (brand vs generic) classification field."""

from apps.llm_ranking.services.extraction_service import (
    EXTRACTION_TEMPLATE,
    EXTRACTION_VERSION,
    _normalise,
)


def _raw(**competitor):
    entry = {"name": "Thing", "position": 1, "linked": False,
             "sentiment": "neutral", "domain": None}
    entry.update(competitor)
    return {
        "target_mentioned": False,
        "target_position": None,
        "target_linked": False,
        "target_sentiment": "not_mentioned",
        "target_context": "",
        "competitors_mentioned": [entry],
        "primary_recommendation": None,
        "citations": [],
    }


class TestKindNormalisation:
    def test_version_bumped(self):
        assert EXTRACTION_VERSION == "v4"

    def test_prompt_teaches_the_distinction(self):
        assert '"kind": "brand" | "generic"' in EXTRACTION_TEMPLATE
        assert "generic" in EXTRACTION_TEMPLATE

    def test_explicit_kinds_pass_through(self):
        out = _normalise(_raw(kind="generic"))
        assert out["competitors_mentioned"][0]["kind"] == "generic"
        out = _normalise(_raw(kind="brand"))
        assert out["competitors_mentioned"][0]["kind"] == "brand"

    def test_missing_or_invalid_kind_defaults_to_brand(self):
        out = _normalise(_raw())
        assert out["competitors_mentioned"][0]["kind"] == "brand"
        out = _normalise(_raw(kind="banana"))
        assert out["competitors_mentioned"][0]["kind"] == "brand"
