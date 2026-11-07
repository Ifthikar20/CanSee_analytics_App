"""Tests for the per-user RAG retriever."""
import pytest

from apps.accounts.tests.factories import UserFactory
from apps.rag.models import KnowledgeChunk, KnowledgeSource
from apps.rag.services.embedder import embed_one
from apps.rag.services.retriever import retrieve, retrieve_context_block
from apps.websites.tests.factories import WebsiteFactory


@pytest.fixture
def no_openai(settings):
    """Force the deterministic-fallback embedder so tests don't hit OpenAI."""
    settings.OPENAI_API_KEY = ""


def _seed_chunks(user, website, texts):
    # Hash the full first text so two seed calls with the same 10-char
    # prefix (e.g. "Cansee analytics blog" vs "Cansee analytics home")
    # produce distinct URLs and don't collide on the (user, website, url)
    # unique constraint.
    import hashlib
    slug = hashlib.sha1(texts[0].encode("utf-8")).hexdigest()[:12]
    source = KnowledgeSource.objects.create(
        user=user, website=website,
        url=f"https://example.com/{slug}",
        kind=KnowledgeSource.KIND_HOMEPAGE,
        status=KnowledgeSource.STATUS_READY,
    )
    for i, text in enumerate(texts):
        vec, model, dim = embed_one(text)
        KnowledgeChunk.objects.create(
            source=source, user=user, website=website,
            chunk_index=i, text=text, embedding=vec,
            embedding_model=model, embedding_dim=dim,
            section_label="seed",
        )
    source.chunk_count = len(texts)
    source.save(update_fields=["chunk_count"])
    return source


@pytest.mark.django_db
class TestRetrieve:
    def test_retrieves_relevant_chunk_above_unrelated(self, no_openai):
        user = UserFactory()
        website = WebsiteFactory()
        _seed_chunks(user, website, [
            "Cansee analytics platform with funnel tracking and dashboards",
            "Recipes for chocolate cake and dessert ideas",
            "Cansee integrates with Slack and Salesforce for SaaS teams",
        ])
        hits = retrieve(
            user=user, website=website,
            query="analytics dashboard for SaaS", top_k=2,
        )
        assert len(hits) >= 1
        # The top hit should be about analytics, not cake.
        assert "analytics" in hits[0].text.lower() or "cansee" in hits[0].text.lower()
        assert "cake" not in hits[0].text.lower()

    def test_scoped_to_user_and_website(self, no_openai):
        user_a = UserFactory()
        user_b = UserFactory()
        website = WebsiteFactory()
        _seed_chunks(user_a, website, ["Cansee analytics platform"])
        # User B should see nothing — chunks belong to user A.
        hits = retrieve(user=user_b, website=website, query="analytics", top_k=5)
        assert hits == []

    def test_empty_query_returns_empty(self, no_openai):
        user = UserFactory()
        website = WebsiteFactory()
        assert retrieve(user=user, website=website, query="") == []
        assert retrieve(user=user, website=website, query="   ") == []

    def test_kinds_filter(self, no_openai):
        user = UserFactory()
        website = WebsiteFactory()
        src_blog = _seed_chunks(user, website, ["Cansee analytics blog post"])
        src_blog.kind = KnowledgeSource.KIND_BLOG
        src_blog.save(update_fields=["kind"])
        _seed_chunks(user, website, ["Cansee analytics homepage copy"])
        hits = retrieve(
            user=user, website=website, query="Cansee analytics",
            top_k=5, kinds=[KnowledgeSource.KIND_BLOG],
        )
        assert all(h.source_kind == KnowledgeSource.KIND_BLOG for h in hits)


@pytest.mark.django_db
class TestRetrieveContextBlock:
    def test_returns_empty_when_no_corpus(self, no_openai):
        user = UserFactory()
        website = WebsiteFactory()
        assert retrieve_context_block(
            user=user, website=website, query="anything",
        ) == ""

    def test_block_contains_knowledge_base_header(self, no_openai):
        user = UserFactory()
        website = WebsiteFactory()
        _seed_chunks(user, website, ["Cansee is a SaaS analytics platform"])
        block = retrieve_context_block(
            user=user, website=website, query="SaaS analytics",
        )
        assert "KNOWLEDGE BASE" in block
        assert "Cansee" in block
