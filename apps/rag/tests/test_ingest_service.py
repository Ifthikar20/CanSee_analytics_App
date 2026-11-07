"""
Tests for ingest_service.

The HTTP scraper (`scan_domain`) and the embedder are mocked at their
module boundaries so the tests stay hermetic. The DB and chunker run
for real because their behaviour is core to the contract under test
(unique-source semantics, content-hash short-circuit, transactional
chunk replacement).
"""
from unittest.mock import patch

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.rag.models import KnowledgeChunk, KnowledgeSource
from apps.rag.services import ingest_service
from apps.websites.tests.factories import WebsiteFactory


def _fake_scan(text: str, business_name: str = "Example"):
    return {
        "success": True,
        "business_name": business_name,
        "content_summary": text,
        "products": [], "features": [], "selling_points": [],
        "topics": [], "industry": "", "url": "https://example.com",
    }


@pytest.fixture
def no_openai(settings):
    """Force the deterministic-fallback embedder."""
    settings.OPENAI_API_KEY = ""


@pytest.fixture
def user_and_website(db):
    return UserFactory(), WebsiteFactory()


@pytest.mark.django_db
class TestIngestURL:
    def test_creates_source_and_chunks(self, no_openai, user_and_website):
        user, website = user_and_website
        with patch.object(
            ingest_service, "scan_domain",
            return_value=_fake_scan("Cansee is an analytics platform " * 80),
        ):
            result = ingest_service.ingest_url(
                user=user, website=website,
                url="https://example.com",
                kind=KnowledgeSource.KIND_HOMEPAGE,
            )
        assert result.status == KnowledgeSource.STATUS_READY
        assert result.chunk_count > 0
        assert result.skipped_unchanged is False

        source = KnowledgeSource.objects.get(user=user, website=website,
                                             url="https://example.com")
        assert source.chunk_count > 0
        assert source.kind == KnowledgeSource.KIND_HOMEPAGE
        chunks = list(KnowledgeChunk.objects.filter(source=source))
        assert len(chunks) == source.chunk_count
        assert all(c.embedding for c in chunks)

    def test_short_circuits_when_content_unchanged(
        self, no_openai, user_and_website,
    ):
        user, website = user_and_website
        text = "Cansee is an analytics platform " * 80
        with patch.object(
            ingest_service, "scan_domain", return_value=_fake_scan(text),
        ):
            first = ingest_service.ingest_url(
                user=user, website=website, url="https://example.com",
                kind=KnowledgeSource.KIND_HOMEPAGE,
            )
            assert first.skipped_unchanged is False
            chunks_before = KnowledgeChunk.objects.filter(
                source_id=first.source_id,
            ).count()

            second = ingest_service.ingest_url(
                user=user, website=website, url="https://example.com",
                kind=KnowledgeSource.KIND_HOMEPAGE,
            )
        assert second.skipped_unchanged is True
        assert second.status == KnowledgeSource.STATUS_READY
        # No chunks rebuilt, no duplicates created.
        chunks_after = KnowledgeChunk.objects.filter(
            source_id=first.source_id,
        ).count()
        assert chunks_after == chunks_before

    def test_re_embeds_when_content_changes(self, no_openai, user_and_website):
        user, website = user_and_website
        with patch.object(
            ingest_service, "scan_domain",
            return_value=_fake_scan("first version of the page " * 50),
        ):
            first = ingest_service.ingest_url(
                user=user, website=website, url="https://example.com",
            )
        with patch.object(
            ingest_service, "scan_domain",
            return_value=_fake_scan("totally different content now " * 50),
        ):
            second = ingest_service.ingest_url(
                user=user, website=website, url="https://example.com",
            )
        assert second.skipped_unchanged is False
        # Old chunks are deleted and replaced — content_hash now matches the
        # second version, so a third unchanged ingest would short-circuit.
        source = KnowledgeSource.objects.get(id=first.source_id)
        chunks = KnowledgeChunk.objects.filter(source=source)
        text_blob = " ".join(c.text for c in chunks)
        assert "totally different" in text_blob
        assert "first version" not in text_blob

    def test_failed_scrape_marks_source_failed(self, user_and_website):
        user, website = user_and_website
        with patch.object(
            ingest_service, "scan_domain",
            return_value={"success": False, "error": "404"},
        ):
            result = ingest_service.ingest_url(
                user=user, website=website, url="https://example.com",
            )
        assert result.status == KnowledgeSource.STATUS_FAILED
        assert result.chunk_count == 0
        assert "404" in (result.error or "")
        source = KnowledgeSource.objects.get(id=result.source_id)
        assert source.status == KnowledgeSource.STATUS_FAILED

    def test_kind_upgrades_on_subsequent_call(self, no_openai, user_and_website):
        user, website = user_and_website
        with patch.object(
            ingest_service, "scan_domain",
            return_value=_fake_scan("page text " * 80),
        ):
            ingest_service.ingest_url(
                user=user, website=website, url="https://example.com",
                kind=KnowledgeSource.KIND_OTHER,
            )
            ingest_service.ingest_url(
                user=user, website=website, url="https://example.com",
                kind=KnowledgeSource.KIND_BLOG,
            )
        source = KnowledgeSource.objects.get(
            user=user, website=website, url="https://example.com",
        )
        assert source.kind == KnowledgeSource.KIND_BLOG

    def test_provided_text_skips_scrape(self, no_openai, user_and_website):
        user, website = user_and_website
        # Patch scan_domain so the test fails loudly if the path is taken.
        with patch.object(
            ingest_service, "scan_domain", side_effect=AssertionError("scraper called"),
        ):
            result = ingest_service.ingest_url(
                user=user, website=website,
                url="https://example.com",
                title="Manual",
                text="Inline text passed by an audit's enrichment step. " * 60,
            )
        assert result.status == KnowledgeSource.STATUS_READY
        assert result.chunk_count > 0


@pytest.mark.django_db
class TestIngestAuditContext:
    def test_persists_under_pseudo_url(self, no_openai, user_and_website):
        user, website = user_and_website
        result = ingest_service.ingest_audit_context(
            user=user, website=website, audit_id="abc-123",
            llm_context="=== MAIN WEBSITE ===\nlots of business intel\n" * 30,
        )
        assert result is not None
        assert result.status == KnowledgeSource.STATUS_READY
        source = KnowledgeSource.objects.get(id=result.source_id)
        assert source.url.startswith("audit://")
        assert source.kind == KnowledgeSource.KIND_AUDIT_CONTEXT

    def test_blank_context_returns_none(self, no_openai, user_and_website):
        user, website = user_and_website
        assert ingest_service.ingest_audit_context(
            user=user, website=website, audit_id="abc",
            llm_context="   ",
        ) is None
