"""Region routing into provider requests / prompts (no network)."""
from apps.llm_ranking.services.regions import (
    get_region,
    region_for_country,
    region_system_instruction,
    supported_countries,
)


class _Resp:
    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": "hi"}}], "usage": {}}


def test_perplexity_passes_user_location(monkeypatch, settings):
    settings.PERPLEXITY_API_KEY = "test-key"
    import apps.llm_ranking.providers.perplexity as pmod

    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(json or {})
        return _Resp()

    monkeypatch.setattr(pmod.requests, "post", fake_post)
    result = pmod.PerplexityProvider()._call(
        prompt="best apps to save money", system_prompt="", region="bh",
    )
    assert result.succeeded
    # Bahrain is now in the catalog and routes natively for Perplexity.
    assert captured["web_search_options"]["user_location"]["country"] == "BH"


class TestRegionSystemInstruction:
    def test_includes_country_name(self):
        instr = region_system_instruction("bh")
        assert "Bahrain" in instr
        instr_ru = region_system_instruction("ru")
        assert "Russia" in instr_ru

    def test_global_and_unknown_are_empty(self):
        assert region_system_instruction("global") == ""
        assert region_system_instruction("zz") == ""
        assert region_system_instruction("") == ""


class TestCatalogCoverage:
    def test_bahrain_and_gulf_present(self):
        for iso in ("BH", "AE", "SA", "QA", "KW"):
            assert region_for_country(iso) == iso.lower()
            assert get_region(iso.lower()).perplexity_country == iso
        codes = {c["code"] for c in supported_countries()}
        assert {"BH", "AE", "SA", "RU", "UA", "US"} <= codes
