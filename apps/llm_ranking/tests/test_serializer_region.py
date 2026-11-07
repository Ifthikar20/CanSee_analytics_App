"""
Tests for the audit serializers' region + citation_countries handling.

These cover the API contract: the input serializer must accept and
default ``region``, and the output serializers must surface the new
fields so the dashboard can consume them.
"""
import pytest

from apps.llm_ranking.api.v1.serializers import (
    LLMRankingAuditListSerializer,
    LLMRankingAuditSerializer,
    RunAuditSerializer,
)
from apps.llm_ranking.tests.factories import LLMRankingAuditFactory


class TestRunAuditSerializerRegion:
    def test_defaults_to_global(self):
        ser = RunAuditSerializer(data={})
        assert ser.is_valid(), ser.errors
        assert ser.validated_data["region"] == "global"

    def test_accepts_known_region(self):
        ser = RunAuditSerializer(data={"region": "in"})
        assert ser.is_valid(), ser.errors
        assert ser.validated_data["region"] == "in"

    def test_rejects_unknown_region(self):
        ser = RunAuditSerializer(data={"region": "zz"})
        assert ser.is_valid() is False
        assert "region" in ser.errors

    def test_accepts_all_supported_regions(self):
        for code in ("global", "us", "ca", "in", "uk", "de", "au"):
            ser = RunAuditSerializer(data={"region": code})
            assert ser.is_valid(), f"{code}: {ser.errors}"


@pytest.mark.django_db
class TestAuditOutputSerializers:
    def test_detail_serializer_exposes_region_and_citations(self):
        audit = LLMRankingAuditFactory(
            region="in",
            citation_countries={"IN": 5, "US": 2},
            mention_rate_smoothed=42.7,
            brand_strengths={"Cansee": 1.0, "Mixpanel": 0.6},
        )
        data = LLMRankingAuditSerializer(audit).data
        assert data["region"] == "in"
        assert data["citation_countries"] == {"IN": 5, "US": 2}
        assert data["mention_rate_smoothed"] == 42.7
        assert data["brand_strengths"]["Cansee"] == 1.0

    def test_list_serializer_exposes_region_and_citations(self):
        audit = LLMRankingAuditFactory(
            region="ca", citation_countries={"CA": 3},
        )
        data = LLMRankingAuditListSerializer(audit).data
        assert data["region"] == "ca"
        assert data["citation_countries"] == {"CA": 3}

    def test_defaults_render_when_unset(self):
        audit = LLMRankingAuditFactory()
        data = LLMRankingAuditSerializer(audit).data
        assert data["region"] == "global"
        assert data["citation_countries"] == {}
        assert data["brand_strengths"] == {}
