"""Sample prompts must be built from what the site actually is — never the
old generic "software" default for a non-software business."""
import json
from unittest.mock import MagicMock, patch

import pytest

from apps.prompt_library.services.sample_prompts import build_sample_prompts
from apps.websites.tests.factories import WebsiteFactory

pytestmark = pytest.mark.django_db

CAR_SCAN = {
    "success": True,
    "industry": "automotive",
    "description": "Alerts you to the best deals on used cars near you.",
    "products": ["car price alerts", "deal tracking"],
}


def _unconfigured_llm():
    """ClaudeUtility stub with no API key → forces the template fallback."""
    fake = MagicMock()
    fake.is_configured.return_value = False
    return fake


class TestScanFirst:
    def test_blank_site_is_scanned_and_persisted(self):
        website = WebsiteFactory(
            name="Car Signal", industry="", description="",
            url="https://trycarsignal.com/",
        )
        with patch("apps.llm_ranking.services.domain_scanner.scan_domain", return_value=CAR_SCAN), \
             patch("apps.prompt_library.services.sample_prompts.ClaudeUtility", return_value=_unconfigured_llm()):
            result = build_sample_prompts(website, count=6, user=website.user)

        website.refresh_from_db()
        assert website.industry == "automotive"
        assert "used cars" in website.description
        # Template fallback fired (LLM unconfigured), but with the REAL industry.
        assert result["source"] == "template"
        joined = " ".join(i["text"].lower() for i in result["items"])
        assert "software" not in joined
        assert "automotive" in joined

    def test_already_classified_skips_scan(self):
        website = WebsiteFactory(
            name="Car Signal", industry="automotive",
            description="Used-car deal alerts.", url="https://trycarsignal.com/",
        )
        with patch("apps.llm_ranking.services.domain_scanner.scan_domain") as scan, \
             patch("apps.prompt_library.services.sample_prompts.ClaudeUtility", return_value=_unconfigured_llm()):
            build_sample_prompts(website, count=4, user=website.user)
        scan.assert_not_called()


class TestLLMGrounding:
    def test_llm_prompts_are_used_when_configured(self):
        website = WebsiteFactory(
            name="Car Signal", industry="automotive",
            description="Alerts on used-car deals.", url="https://trycarsignal.com/",
        )
        fake = MagicMock()
        fake.is_configured.return_value = True
        fake.query.return_value = MagicMock(succeeded=True, text=json.dumps([
            "What's the best way to get alerts on used car price drops?",
            "How do I find the cheapest deals on used cars near me?",
            "Which services track used car prices for buyers?",
        ]))
        with patch("apps.prompt_library.services.sample_prompts.ClaudeUtility", return_value=fake):
            result = build_sample_prompts(website, count=5, user=website.user)

        assert result["source"] == "llm"
        assert result["items"]
        joined = " ".join(i["text"].lower() for i in result["items"])
        assert "car" in joined
        assert "software" not in joined


class TestUnclassifiable:
    def test_unscannable_blank_site_returns_none(self):
        website = WebsiteFactory(
            name="Mystery", industry="", description="",
            url="https://unreachable.example/",
        )
        with patch("apps.llm_ranking.services.domain_scanner.scan_domain", return_value={"success": False}):
            result = build_sample_prompts(website, count=6, user=website.user)
        assert result["source"] == "none"
        assert result["items"] == []


class TestEndpoint:
    def test_endpoint_422_when_unclassifiable(self):
        from rest_framework.test import APIClient
        website = WebsiteFactory(
            name="Mystery", industry="", description="",
            url="https://unreachable.example/",
        )
        client = APIClient()
        client.force_authenticate(user=website.user)
        url = f"/api/v1/prompt-library/websites/{website.id}/prompts/generate-samples/"
        with patch("apps.llm_ranking.services.domain_scanner.scan_domain", return_value={"success": False}):
            resp = client.post(url, {"count": 8}, format="json")
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "site_not_classified"
