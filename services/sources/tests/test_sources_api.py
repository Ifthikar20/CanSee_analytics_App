"""HTTP surface of the sources service (auth, search mapping, read dispatch)."""

from unittest.mock import MagicMock, patch

import requests
from fastapi.testclient import TestClient

from services.sources.main import app

client = TestClient(app)


def _auth(token="secret"):
    return {"Authorization": f"Bearer {token}"}


def _resp(payload, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=payload)
    return resp


def test_health_is_open():
    assert client.get("/health").json() == {"status": "ok"}


def test_missing_server_token_refuses(monkeypatch):
    monkeypatch.delenv("SOURCES_AUTH_TOKEN", raising=False)
    assert client.post("/v1/search", json={"query": "q"}, headers=_auth()).status_code == 503


def test_wrong_token_rejected(monkeypatch):
    monkeypatch.setenv("SOURCES_AUTH_TOKEN", "secret")
    assert client.post("/v1/search", json={"query": "q"},
                       headers=_auth("bad")).status_code == 401


def test_search_not_configured(monkeypatch):
    monkeypatch.setenv("SOURCES_AUTH_TOKEN", "secret")
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
    resp = client.post("/v1/search", json={"query": "q"}, headers=_auth())
    assert resp.status_code == 503
    assert resp.json()["error"] == "not_configured"


def test_search_success(monkeypatch):
    monkeypatch.setenv("SOURCES_AUTH_TOKEN", "secret")
    monkeypatch.setenv("PERPLEXITY_API_KEY", "pplx-test")
    payload = {"results": [
        {"url": "https://www.reddit.com/r/x/comments/1/a/", "title": "T",
         "snippet": "s", "date": "2025-05-10"},
    ]}
    with patch.object(requests, "post", return_value=_resp(payload)):
        resp = client.post("/v1/search", json={"query": "q"}, headers=_auth())
    assert resp.status_code == 200
    row = resp.json()["results"][0]
    assert row["rank"] == 1
    assert row["domain"] == "reddit.com"
    assert row["date"] == "2025-05-10"


def test_search_upstream_failure_maps_to_502(monkeypatch):
    monkeypatch.setenv("SOURCES_AUTH_TOKEN", "secret")
    monkeypatch.setenv("PERPLEXITY_API_KEY", "pplx-test")
    with patch.object(requests, "post",
                      side_effect=requests.ConnectionError("down")):
        resp = client.post("/v1/search", json={"query": "q"}, headers=_auth())
    assert resp.status_code == 502
    assert resp.json()["error"] == "upstream_request_failed"


def test_read_dispatches_reddit(monkeypatch):
    monkeypatch.setenv("SOURCES_AUTH_TOKEN", "secret")
    reddit_payload = [
        {"data": {"children": [{"data": {
            "title": "Best bagels", "subreddit": "Dallas", "score": 10,
            "selftext": "",
        }}]}},
        {"data": {"children": [
            {"kind": "t1", "data": {"body": "Starship.", "score": 42, "replies": ""}},
        ]}},
    ]
    with patch.object(requests, "get", return_value=_resp(reddit_payload)):
        resp = client.post(
            "/v1/read",
            json={"url": "https://www.reddit.com/r/Dallas/comments/abc/best/"},
            headers=_auth(),
        )
    body = resp.json()
    assert body["status"] == "ok"
    assert body["kind"] == "reddit"
    assert "[+42] Starship." in body["text"]


def test_read_yelp_without_key_is_blocked(monkeypatch):
    monkeypatch.setenv("SOURCES_AUTH_TOKEN", "secret")
    monkeypatch.delenv("YELP_API_KEY", raising=False)
    resp = client.post("/v1/read", json={"url": "https://www.yelp.com/biz/x"},
                       headers=_auth())
    body = resp.json()
    assert body["status"] == "blocked"
    assert "YELP_API_KEY" in body["detail"]
