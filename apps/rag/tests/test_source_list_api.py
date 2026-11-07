"""
Contract tests for the paginated + filterable Knowledge Source list view.

The Brand Input page depends on:
- ``stats`` counts scoped to the whole (user, website) corpus, not
  the current filter (badges must not double-narrow when the user
  clicks one).
- ``status``, ``kind``, ``domain``, and ``search`` filters composing.
- Standard DRF pagination envelope alongside ``stats``.
- Cross-tenant isolation — a user filtering by ``domain=example.com``
  must never see another user's rows for the same host.
"""
import pytest
from rest_framework.test import APIClient

from apps.accounts.tests.factories import UserFactory
from apps.rag.models import KnowledgeSource
from apps.websites.tests.factories import WebsiteFactory


def _make(user, website, url, *, status=KnowledgeSource.STATUS_READY,
          kind=KnowledgeSource.KIND_OTHER, title=""):
    return KnowledgeSource.objects.create(
        user=user, website=website, url=url,
        status=status, kind=kind, title=title,
    )


@pytest.fixture
def client(db):
    return APIClient()


@pytest.fixture
def user(db):
    u = UserFactory()
    return u


@pytest.fixture
def website(user):
    # Owned by the authenticated user — TenantScopedAPIView 404s
    # requests for websites the caller does not own.
    return WebsiteFactory(user=user)


@pytest.mark.django_db
class TestSourceListStats:
    """`stats` must count the whole (user, website) corpus, not the filter."""

    def _url(self, website):
        return f"/api/v1/rag/{website.id}/sources/"

    def test_stats_reflect_whole_corpus_even_when_filtered(
        self, client, user, website,
    ):
        _make(user, website, "https://a.example.com/",
              status=KnowledgeSource.STATUS_READY)
        _make(user, website, "https://a.example.com/x",
              status=KnowledgeSource.STATUS_READY)
        _make(user, website, "https://a.example.com/y",
              status=KnowledgeSource.STATUS_PENDING)
        _make(user, website, "https://a.example.com/z",
              status=KnowledgeSource.STATUS_FAILED)

        client.force_authenticate(user=user)
        r = client.get(self._url(website), {"status": "ready"})
        assert r.status_code == 200
        # EnvelopeRenderer wraps paginated responses: results in .data,
        # count/next/previous/stats in .meta.
        body = r.json()
        assert body["success"] is True
        assert len(body["data"]) == 2  # filter narrowed to ready
        # `stats` still counts the whole corpus so the filter chips
        # display accurate totals.
        assert body["meta"]["stats"] == {
            "total": 4, "ready": 2, "ingesting": 0, "pending": 1, "failed": 1,
        }


@pytest.mark.django_db
class TestSourceListFilters:
    """Filter params compose and are cross-user isolated."""

    def _url(self, website):
        return f"/api/v1/rag/{website.id}/sources/"

    def test_domain_filter_matches_scheme_and_trailing_slash_variants(
        self, client, user, website,
    ):
        _make(user, website, "https://docs.example.com/",
              status=KnowledgeSource.STATUS_READY)
        _make(user, website, "https://docs.example.com/getting-started",
              status=KnowledgeSource.STATUS_READY)
        _make(user, website, "http://docs.example.com",
              status=KnowledgeSource.STATUS_READY)
        # Different host — must not appear.
        _make(user, website, "https://blog.example.com/post-1",
              status=KnowledgeSource.STATUS_READY)

        client.force_authenticate(user=user)
        r = client.get(self._url(website), {"domain": "docs.example.com"})
        assert r.status_code == 200
        urls = [row["url"] for row in r.json()["data"]]
        assert set(urls) == {
            "https://docs.example.com/",
            "https://docs.example.com/getting-started",
            "http://docs.example.com",
        }

    def test_domain_paste_filter_only_returns_pasted_sources(
        self, client, user, website,
    ):
        _make(user, website, "https://a.example.com/",
              status=KnowledgeSource.STATUS_READY)
        _make(user, website, f"paste://{website.id}/abc123",
              status=KnowledgeSource.STATUS_READY,
              title="Refund policy")

        client.force_authenticate(user=user)
        r = client.get(self._url(website), {"domain": "paste"})
        assert r.status_code == 200
        rows = r.json()["data"]
        assert len(rows) == 1
        assert rows[0]["is_paste"] is True
        assert rows[0]["domain"] == "paste"

    def test_search_matches_title_and_url_substring(
        self, client, user, website,
    ):
        _make(user, website, "https://example.com/pricing",
              title="")
        _make(user, website, "https://example.com/other",
              title="Return and refund policy")
        _make(user, website, "https://example.com/blog/post",
              title="Not related")

        client.force_authenticate(user=user)
        r = client.get(self._url(website), {"search": "refund"})
        rows = r.json()["data"]
        assert [row["url"] for row in rows] == ["https://example.com/other"]

        r = client.get(self._url(website), {"search": "pricing"})
        rows = r.json()["data"]
        assert [row["url"] for row in rows] == ["https://example.com/pricing"]

    def test_kind_filter(self, client, user, website):
        _make(user, website, "https://example.com/",
              kind=KnowledgeSource.KIND_HOMEPAGE)
        _make(user, website, "https://example.com/docs",
              kind=KnowledgeSource.KIND_DOCS)

        client.force_authenticate(user=user)
        r = client.get(self._url(website), {"kind": "docs"})
        rows = r.json()["data"]
        assert [row["kind"] for row in rows] == ["docs"]

    def test_other_users_rows_never_leak_via_domain_filter(
        self, client, user, website,
    ):
        # Same host, different owner — critical isolation guarantee.
        other = UserFactory()
        _make(other, website, "https://example.com/secret",
              status=KnowledgeSource.STATUS_READY,
              title="OTHER USER PRIVATE")
        _make(user, website, "https://example.com/mine",
              status=KnowledgeSource.STATUS_READY)

        client.force_authenticate(user=user)
        r = client.get(self._url(website), {"domain": "example.com"})
        rows = r.json()["data"]
        assert [row["url"] for row in rows] == ["https://example.com/mine"]


@pytest.mark.django_db
class TestSourceListPagination:
    """Standard DRF pagination shape, stats attached alongside."""

    def _url(self, website):
        return f"/api/v1/rag/{website.id}/sources/"

    def test_page_size_and_meta(self, client, user, website):
        for i in range(30):
            _make(user, website, f"https://example.com/p{i:02d}")

        client.force_authenticate(user=user)
        r = client.get(self._url(website), {"page_size": 10, "page": 2})
        assert r.status_code == 200
        body = r.json()
        assert len(body["data"]) == 10
        assert body["meta"]["count"] == 30
        assert body["meta"]["next"] is not None
        assert body["meta"]["previous"] is not None
        assert body["meta"]["stats"]["total"] == 30

    def test_domain_and_is_paste_serialized(self, client, user, website):
        _make(user, website, "https://docs.example.com/a")
        _make(user, website, f"paste://{website.id}/deadbeef")

        client.force_authenticate(user=user)
        r = client.get(self._url(website))
        rows = r.json()["data"]
        by_url = {row["url"]: row for row in rows}
        assert by_url["https://docs.example.com/a"]["domain"] == "docs.example.com"
        assert by_url["https://docs.example.com/a"]["is_paste"] is False
        paste_key = next(u for u in by_url if u.startswith("paste://"))
        assert by_url[paste_key]["domain"] == "paste"
        assert by_url[paste_key]["is_paste"] is True


@pytest.mark.django_db
class TestReingestView:
    """POST re-queues URL sources; paste sources are refused."""

    def _url(self, website, source_id):
        return f"/api/v1/rag/{website.id}/sources/{source_id}/reingest/"

    def test_reingest_url_source_flips_status_and_queues_task(
        self, client, user, website, monkeypatch,
    ):
        source = _make(user, website, "https://example.com/pricing",
                       status=KnowledgeSource.STATUS_READY,
                       title="Pricing")
        source.chunk_count = 8
        source.error_message = "some prior error"
        source.save(update_fields=["chunk_count", "error_message"])

        called = {}
        def fake_delay(*, user_id, website_id, url, kind, title):
            called["url"] = url
            called["kind"] = kind
            called["user_id"] = user_id
        # Patch the actual task the view imports lazily.
        from apps.rag import tasks
        monkeypatch.setattr(tasks.ingest_url_task, "delay", fake_delay)

        client.force_authenticate(user=user)
        r = client.post(self._url(website, source.id))
        assert r.status_code == 202
        source.refresh_from_db()
        assert source.status == KnowledgeSource.STATUS_PENDING
        assert source.error_message == ""
        assert called["url"] == "https://example.com/pricing"
        assert called["user_id"] == user.id

    def test_reingest_refuses_paste_sources(self, client, user, website):
        source = _make(
            user, website, f"paste://{website.id}/deadbeef",
            status=KnowledgeSource.STATUS_READY,
        )
        client.force_authenticate(user=user)
        r = client.post(self._url(website, source.id))
        assert r.status_code == 400
        # The body is wrapped by the exception handler; we only care
        # that the source was NOT flipped to pending.
        source.refresh_from_db()
        assert source.status == KnowledgeSource.STATUS_READY
