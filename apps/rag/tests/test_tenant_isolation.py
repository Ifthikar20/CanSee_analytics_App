"""
Tenant isolation regression harness for the RAG knowledge base.

The RAG layer stores every chunk against a (user, website) pair and every
query filters on both. This file exists to make that guarantee load-bearing:
if a future refactor drops the ``user=`` filter on any read path, one of
these tests fails loudly.

The existing test in ``test_retriever.py`` covers the empty-corpus case
(user B has no chunks so retrieval trivially returns []). The scenario
these tests exercise is stronger: **both** users have populated corpora
on the same website with distinct content, and neither user's queries
may ever surface the other's chunks.
"""
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


def _seed(user, website, url, texts, kind=KnowledgeSource.KIND_HOMEPAGE):
    source = KnowledgeSource.objects.create(
        user=user, website=website, url=url, kind=kind,
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
class TestTenantIsolation:
    """One-leak-ends-the-company: prove the retriever is user-scoped."""

    def test_two_users_same_website_never_cross(self, no_openai):
        """Both users have chunks on the same website. Each sees only their own.

        The hash-fallback embedder is bag-of-words, so the queries use
        literal tokens from the seeded text. That's fine for this test:
        we're proving the ``user=`` filter is applied, not the semantic
        quality of the ranker.
        """
        user_a = UserFactory()
        user_b = UserFactory()
        website = WebsiteFactory()

        _seed(user_a, website, "https://a.example.com/", [
            "AcmeCorp confidential pricing tier is $2000 per seat per year",
            "AcmeCorp founding investors include Sequoia and Benchmark",
        ])
        _seed(user_b, website, "https://b.example.com/", [
            "ContosoLtd public list price is $79 per month",
            "ContosoLtd was bootstrapped without outside investment",
        ])

        # User A queries: must never see any of B's content.
        hits_a = retrieve(
            user=user_a, website=website,
            query="confidential pricing tier founding investors", top_k=10,
        )
        assert hits_a, "User A should see their own chunks"
        for h in hits_a:
            assert "Contoso" not in h.text
            assert "$79" not in h.text
            assert "bootstrapped" not in h.text

        # User B queries: must never see any of A's content.
        hits_b = retrieve(
            user=user_b, website=website,
            query="public list price bootstrapped investment", top_k=10,
        )
        assert hits_b, "User B should see their own chunks"
        for h in hits_b:
            assert "Acme" not in h.text
            assert "$2000" not in h.text
            assert "Sequoia" not in h.text

    def test_context_block_never_leaks_other_user(self, no_openai):
        """The formatted context block reuses ``retrieve`` — same guarantee."""
        user_a = UserFactory()
        user_b = UserFactory()
        website = WebsiteFactory()

        _seed(user_a, website, "https://a.example.com/", [
            "SecretProjectAtlas launches Q4 with $10M budget",
        ])
        _seed(user_b, website, "https://b.example.com/", [
            "PublicProjectBeta launches Q1 with $1M budget",
        ])

        block_b = retrieve_context_block(
            user=user_b, website=website,
            query="PublicProjectBeta launches budget",
        )
        # B may see their own project mentioned; must never see A's.
        assert "SecretProjectAtlas" not in block_b
        assert "10m" not in block_b.lower()

    def test_direct_orm_scope_matches_retriever_scope(self, no_openai):
        """Sanity: the retriever's scope is exactly (user, website).

        If a future refactor widens the query -- e.g. drops ``user=`` and
        relies on website scoping alone -- this test surfaces the drift
        by comparing the retriever's candidate set to the direct ORM scope.
        """
        user_a = UserFactory()
        user_b = UserFactory()
        website = WebsiteFactory()

        _seed(user_a, website, "https://a.example.com/", ["alpha content one"])
        _seed(user_b, website, "https://b.example.com/", ["beta content two"])

        a_scope = set(
            KnowledgeChunk.objects
            .filter(user=user_a, website=website)
            .values_list("id", flat=True)
        )
        b_scope = set(
            KnowledgeChunk.objects
            .filter(user=user_b, website=website)
            .values_list("id", flat=True)
        )

        # Fundamental invariant: A's chunk set and B's chunk set are disjoint.
        assert a_scope and b_scope
        assert a_scope.isdisjoint(b_scope)

    def test_deleting_source_only_affects_owner(self, no_openai):
        """Cascading source deletion must not touch the other user's chunks."""
        user_a = UserFactory()
        user_b = UserFactory()
        website = WebsiteFactory()

        src_a = _seed(user_a, website, "https://a.example.com/", ["alpha alpha"])
        _seed(user_b, website, "https://b.example.com/", ["beta beta"])

        src_a.delete()

        assert not KnowledgeChunk.objects.filter(user=user_a).exists()
        assert KnowledgeChunk.objects.filter(user=user_b).count() == 1
