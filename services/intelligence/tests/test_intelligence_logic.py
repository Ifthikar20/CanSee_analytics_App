"""Envelope semantics of the shared intelligence logic (no Django, no db)."""

from unittest.mock import MagicMock, patch

from services.intelligence import logic


def _anthropic_resp(payload_json: str):
    block = MagicMock()
    block.text = payload_json
    resp = MagicMock()
    resp.content = [block]
    resp.usage = MagicMock(input_tokens=11, output_tokens=7)
    return resp


def test_analyze_content_no_key_has_no_usage():
    env = logic.analyze_content("text", query="q", target_brand="t",
                                api_key="", model_name="m")
    assert env["result"] == {"brands": [], "error": "no_api_key"}
    assert env["usage"] is None


def test_analyze_content_empty_content_has_no_usage():
    env = logic.analyze_content("   ", query="q", target_brand="t",
                                api_key="k", model_name="m")
    assert env["result"]["error"] == "empty_content"
    assert env["usage"] is None


def test_analyze_content_api_exception_has_no_usage():
    client = MagicMock()
    client.messages.create.side_effect = RuntimeError("boom")
    with patch("anthropic.Anthropic", return_value=client):
        env = logic.analyze_content("text", query="q", target_brand="t",
                                    api_key="k", model_name="m")
    assert env["result"]["brands"] == []
    assert "boom" in env["result"]["error"]
    assert env["usage"] is None


def test_analyze_content_non_json_keeps_usage():
    client = MagicMock()
    client.messages.create.return_value = _anthropic_resp("not json at all")
    with patch("anthropic.Anthropic", return_value=client):
        env = logic.analyze_content("text", query="q", target_brand="t",
                                    api_key="k", model_name="m")
    assert env["result"]["error"] == "non_json_response"
    assert env["usage"] == {"input_tokens": 11, "output_tokens": 7}


def test_analyze_content_success_envelope():
    payload = ('{"brands": [{"name": "X", "mentions": 1, "sentiment": 0.5,'
               ' "weight": 0.4, "quotes": ["good"], "issues": ["pricey"]}],'
               ' "target_brand_present": true, "relevant_to_query": false,'
               ' "summary": "s"}')
    client = MagicMock()
    client.messages.create.return_value = _anthropic_resp(payload)
    with patch("anthropic.Anthropic", return_value=client):
        env = logic.analyze_content("text", query="q", target_brand="X",
                                    api_key="k", model_name="my-model")
    assert env["model"] == "my-model"
    assert env["usage"]["output_tokens"] == 7
    assert env["result"]["brands"][0]["issues"] == ["pricey"]
    assert env["result"]["relevant_to_query"] is False


def test_serp_relevance_fails_open_without_key():
    items = [{"rank": 1, "title": "a", "url": "u"}]
    env = logic.assess_serp_relevance("q", items, api_key="", model_name="m")
    assert env["result"] == {1: {"relevant": True, "reason": ""}}
    assert env["usage"] is None


def test_serp_relevance_fails_open_on_non_json():
    client = MagicMock()
    client.messages.create.return_value = _anthropic_resp("garbage")
    items = [{"rank": 1, "title": "a", "url": "u"}]
    with patch("anthropic.Anthropic", return_value=client):
        env = logic.assess_serp_relevance("q", items, api_key="k", model_name="m")
    assert env["result"][1]["relevant"] is True
    assert env["usage"] is not None


def test_serp_relevance_parses_verdicts():
    payload = ('{"results": [{"rank": 1, "relevant": true, "reason": ""},'
               ' {"rank": 2, "relevant": false, "reason": "handbags"}]}')
    client = MagicMock()
    client.messages.create.return_value = _anthropic_resp(payload)
    items = [{"rank": 1, "title": "a", "url": "u"},
             {"rank": 2, "title": "b", "url": "v"}]
    with patch("anthropic.Anthropic", return_value=client):
        env = logic.assess_serp_relevance("q", items, api_key="k", model_name="m")
    assert env["result"][2] == {"relevant": False, "reason": "handbags"}


def test_resolve_model():
    assert logic.resolve_model("") == logic.DEFAULT_MODEL
    assert logic.resolve_model("custom") == "custom"
