"""HTTP surface of the intelligence service (auth + endpoint contracts)."""

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from services.intelligence.main import app

client = TestClient(app)


def _auth(token="secret"):
    return {"Authorization": f"Bearer {token}"}


def test_health_is_open():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_missing_server_token_refuses(monkeypatch):
    monkeypatch.delenv("INTELLIGENCE_AUTH_TOKEN", raising=False)
    resp = client.post("/v1/analyze-content", json={"text": "t"}, headers=_auth())
    assert resp.status_code == 503


def test_wrong_token_rejected(monkeypatch):
    monkeypatch.setenv("INTELLIGENCE_AUTH_TOKEN", "secret")
    resp = client.post("/v1/analyze-content", json={"text": "t"},
                       headers=_auth("wrong"))
    assert resp.status_code == 401
    resp = client.post("/v1/analyze-content", json={"text": "t"})
    assert resp.status_code == 401


def test_analyze_content_endpoint(monkeypatch):
    monkeypatch.setenv("INTELLIGENCE_AUTH_TOKEN", "secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    block = MagicMock()
    block.text = ('{"brands": [], "target_brand_present": false,'
                  ' "relevant_to_query": true, "summary": "s"}')
    fake_resp = MagicMock(content=[block])
    fake_resp.usage = MagicMock(input_tokens=3, output_tokens=4)
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_resp

    with patch("anthropic.Anthropic", return_value=fake_client):
        resp = client.post(
            "/v1/analyze-content",
            json={"text": "content", "query": "q", "target_brand": "X"},
            headers=_auth(),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"]["summary"] == "s"
    assert body["usage"] == {"input_tokens": 3, "output_tokens": 4}


def test_serp_relevance_endpoint_string_keys(monkeypatch):
    monkeypatch.setenv("INTELLIGENCE_AUTH_TOKEN", "secret")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    resp = client.post(
        "/v1/serp-relevance",
        json={"query": "q", "items": [{"rank": 1, "title": "a", "url": "u"}]},
        headers=_auth(),
    )
    assert resp.status_code == 200
    # JSON object keys are strings on the wire; the Django facade
    # converts them back to ints.
    assert resp.json()["result"] == {"1": {"relevant": True, "reason": ""}}
