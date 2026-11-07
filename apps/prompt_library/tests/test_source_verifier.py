"""Tests for the prompt-source verifier (strict keyword policy)."""

from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase

from apps.prompt_library.services import source_verifier as sv


class PromptKeywordsTests(TestCase):
    def test_strips_stopwords_and_short_tokens(self):
        kws = sv._prompt_keywords("What are the best vineyards in Napa for a wedding?")
        # 'what', 'are', 'the', 'in', 'for', 'a' are stopwords; 'best' too.
        self.assertEqual(set(kws), {"vineyards", "napa", "wedding"})

    def test_dedupes(self):
        kws = sv._prompt_keywords("vineyards Napa vineyards Napa")
        self.assertEqual(kws, ["vineyards", "napa"])

    def test_empty(self):
        self.assertEqual(sv._prompt_keywords(""), [])
        self.assertEqual(sv._prompt_keywords("the and of"), [])


class StripHTMLTests(TestCase):
    def test_drops_scripts_and_tags(self):
        html = (
            "<html><head><style>.x{}</style></head>"
            "<body><script>alert(1)</script>"
            "<p>Napa <b>vineyards</b> for weddings</p></body></html>"
        )
        text = sv._strip_html(html)
        self.assertIn("napa", text)
        self.assertIn("vineyards", text)
        self.assertNotIn("alert(1)", text)
        self.assertNotIn(".x{}", text)


class VerifySourceTests(TestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_verified_when_all_keywords_present(self):
        with patch.object(sv, "_fetch", return_value=
                          "great napa vineyards perfect for weddings"):
            v = sv.verify_source(
                "What are the best vineyards in Napa for a wedding?",
                "https://example.com/forum/post",
            )
        self.assertTrue(v["verified"])
        self.assertEqual(v["matched"], 3)
        self.assertEqual(v["missing"], [])

    def test_rejected_when_keyword_missing(self):
        with patch.object(sv, "_fetch", return_value="random forum chat"):
            v = sv.verify_source(
                "best vineyards in Napa for wedding",
                "https://example.com/forum/post",
            )
        self.assertFalse(v["verified"])
        self.assertIn("vineyards", v["missing"])
        self.assertIn("napa", v["missing"])

    def test_fetch_failure_records_unverified_and_caches(self):
        with patch.object(sv, "_fetch", return_value=None) as m:
            v1 = sv.verify_source("napa vineyards", "https://example.com/x")
            v2 = sv.verify_source("napa vineyards", "https://example.com/x")
        self.assertFalse(v1["verified"])
        self.assertTrue(v1.get("fetch_failed"))
        self.assertTrue(v2["cached"])
        # Second call must hit cache, not re-fetch.
        self.assertEqual(m.call_count, 1)

    def test_empty_prompt_is_unverified(self):
        v = sv.verify_source("", "https://example.com")
        self.assertFalse(v["verified"])

    def test_filter_verified_drops_misses_and_annotates_hits(self):
        rows = [
            {"url": "https://hit.com/a", "title": "Hit"},
            {"url": "https://miss.com/b", "title": "Miss"},
            {"url": "", "title": "No URL"},
        ]

        def fake_fetch(url):
            return "napa vineyards wedding" if "hit.com" in url else "unrelated"

        with patch.object(sv, "_fetch", side_effect=fake_fetch):
            kept = sv.filter_verified("napa vineyards wedding", rows)

        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["url"], "https://hit.com/a")
        self.assertEqual(kept[0]["_verification"]["matched"], 3)
        self.assertEqual(kept[0]["_verification"]["total"], 3)
