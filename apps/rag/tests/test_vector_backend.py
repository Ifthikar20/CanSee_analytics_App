"""RAG_VECTOR_BACKEND=chroma - the embedded vector-index path.

The contract under test: with the index enabled, retrieval returns the
same chunks as the Python cosine loop, tenant isolation holds, filtered
calls keep using the exact path, and index failures degrade to the
Python path instead of breaking retrieval.

Uses the deterministic 256-dim hash embedder (no OPENAI_API_KEY in test
settings), same as the rest of the rag suite. Each test gets a fresh
tmp index directory so collections never leak across tests.
"""
import uuid

import pytest
from django.test import override_settings

pytest.importorskip("chromadb", reason="chromadb not installed")

from apps.rag.models import KnowledgeChunk, KnowledgeSource  # noqa: E402
from apps.rag.services import vector_backends  # noqa: E402
from apps.rag.services.embedder import embed_texts  # noqa: E402
from apps.rag.services.retriever import retrieve  # noqa: E402

TEXTS = [
    "Cansee tracks AI visibility for e-commerce brands.",
    "Our returns policy allows refunds within thirty days of delivery.",
    "The engineering blog covers Django performance tuning.",
    "Pricing starts at forty-nine dollars per month on the Pro plan.",
    "Contact support through the in-app chat or by email.",
]


@pytest.fixture
def website(db):
    from apps.accounts.models import User
    from apps.websites.models import Website

    user = User.objects.create_user(
        email=f"rag-{uuid.uuid4().hex[:8]}@example.com",
        password="TestPass123!",
        full_name="Rag Tester",
    )
    return Website.objects.create(
        user=user, name="RagCo", url="https://ragco.example.com",
        industry="SaaS", pixel_key=uuid.uuid4(), is_active=True,
    )


@pytest.fixture
def chroma_settings(tmp_path):
    vector_backends.reset_backend_for_tests()
    # OPENAI_API_KEY blanked per house convention (see test_embedder,
    # test_retriever): the developer's .env can carry a real key, and the
    # suite must always use the deterministic hash embedder, never the
    # network.
    with override_settings(
        RAG_VECTOR_BACKEND="chroma",
        RAG_CHROMA_PATH=str(tmp_path / "idx"),
        OPENAI_API_KEY="",
    ):
        yield
    vector_backends.reset_backend_for_tests()


def _seed(website, texts=TEXTS):
    """Create one source with one chunk per text, mirrored to the index
    exactly the way ingest_service does it."""
    source = KnowledgeSource.objects.create(
        user=website.user, website=website,
        url=f"https://ragco.example.com/{uuid.uuid4().hex[:6]}",
        kind=KnowledgeSource.KIND_DOCS, status=KnowledgeSource.STATUS_READY,
    )
    vectors, model, dim = embed_texts(list(texts))
    rows = [
        KnowledgeChunk(
            source=source, user=website.user, website=website,
            chunk_index=i, text=t, embedding=vectors[i],
            embedding_model=model, embedding_dim=dim,
        )
        for i, t in enumerate(texts)
    ]
    KnowledgeChunk.objects.bulk_create(rows)

    backend = vector_backends.get_backend()
    if backend is not None:
        backend.replace_source(
            website_id=website.id, source_id=source.id,
            chunk_ids=[r.id for r in rows], vectors=vectors, dim=dim,
        )
    return source, rows


@pytest.mark.django_db
class TestChromaBackendRetrieval:
    def test_backend_and_python_paths_agree(self, website, chroma_settings):
        """The money assertion: same corpus, same query, same hits."""
        _seed(website)
        # The hash embedder is bag-of-words, not semantic: pick a query
        # whose tokens overlap exactly one corpus text so the expected
        # top hit is deterministic.
        query = "returns policy refunds delivery"

        with_backend = retrieve(user=website.user, website=website, query=query)

        vector_backends.reset_backend_for_tests()
        with override_settings(RAG_VECTOR_BACKEND="python", OPENAI_API_KEY=""):
            plain = retrieve(user=website.user, website=website, query=query)

        assert [h.chunk_id for h in with_backend] == [h.chunk_id for h in plain]
        assert with_backend, "expected at least one hit for the refunds query"
        assert "refunds" in with_backend[0].text

    def test_scores_match_the_python_cosine(self, website, chroma_settings):
        _seed(website)
        query = "django performance"

        with_backend = retrieve(user=website.user, website=website, query=query)
        vector_backends.reset_backend_for_tests()
        with override_settings(RAG_VECTOR_BACKEND="python", OPENAI_API_KEY=""):
            plain = retrieve(user=website.user, website=website, query=query)

        for hb, hp in zip(with_backend, plain, strict=True):
            assert hb.score == pytest.approx(hp.score, abs=1e-4)

    def test_tenant_isolation_across_websites(self, website, chroma_settings):
        from apps.websites.models import Website

        other = Website.objects.create(
            user=website.user, name="OtherCo",
            url="https://otherco.example.com", industry="SaaS",
            pixel_key=uuid.uuid4(), is_active=True,
        )
        _seed(website)
        _seed(other, texts=["Entirely different corpus about sailing boats."])

        hits = retrieve(user=website.user, website=other, query="sailing boats")
        assert hits
        assert all("sailing" in h.text for h in hits)

    def test_filtered_calls_use_the_exact_python_path(self, website, chroma_settings):
        """Filters live in Postgres, so a kinds= call must not touch the
        index. Poison the backend: if it is queried, this test fails."""
        source, _ = _seed(website)
        backend = vector_backends.get_backend()

        def boom(**kwargs):
            raise AssertionError("index must not be queried for filtered calls")

        backend.query = boom
        hits = retrieve(
            user=website.user, website=website, query="refunds",
            kinds=[KnowledgeSource.KIND_DOCS],
        )
        assert hits, "filtered retrieval should work entirely from Postgres"

    def test_backend_failure_degrades_to_python_path(self, website, chroma_settings):
        _seed(website)
        backend = vector_backends.get_backend()

        def boom(**kwargs):
            raise RuntimeError("index corrupted")

        backend.query = boom
        hits = retrieve(user=website.user, website=website, query="refunds")
        assert hits, "backend failure must degrade, not break retrieval"

    def test_reingest_replaces_stale_vectors(self, website, chroma_settings):
        source, rows = _seed(website)
        backend = vector_backends.get_backend()

        new_texts = ["Completely new content about invoice exports."]
        vectors, _model, dim = embed_texts(new_texts)
        KnowledgeChunk.objects.filter(source=source).delete()
        new_rows = [KnowledgeChunk.objects.create(
            source=source, user=website.user, website=website,
            chunk_index=0, text=new_texts[0], embedding=vectors[0],
            embedding_model=_model, embedding_dim=dim,
        )]
        backend.replace_source(
            website_id=website.id, source_id=source.id,
            chunk_ids=[r.id for r in new_rows], vectors=vectors, dim=dim,
        )

        coll = backend._collection(website.id, dim)
        assert coll.count() == 1, "old vectors must be gone after replace"

    def test_orphaned_index_ids_are_dropped_at_query_time(self, website, chroma_settings):
        """Delete chunks from Postgres but not the index: hits must not
        resurrect them, because rows are re-fetched by id."""
        source, rows = _seed(website)
        KnowledgeChunk.objects.filter(source=source).delete()

        hits = retrieve(user=website.user, website=website, query="refunds")
        assert hits == []


@pytest.mark.django_db
class TestCrossTenantHardening:
    """The index must never be able to leak one tenant's content into
    another tenant's answers - even when the index itself is wrong.

    The retriever's design makes Postgres the authority: index hits are
    re-fetched with the (user, website) filter, so a polluted collection
    can name foreign chunk ids all it likes and they drop out before
    ranking. These tests attack exactly that seam.
    """

    def _tenant(self, tag):
        from apps.accounts.models import User
        from apps.websites.models import Website

        user = User.objects.create_user(
            email=f"iso-{tag}-{uuid.uuid4().hex[:6]}@example.com",
            password="TestPass123!", full_name=f"Iso {tag}",
        )
        site = Website.objects.create(
            user=user, name=f"Site{tag}",
            url=f"https://{tag}.example.com", industry="SaaS",
            pixel_key=uuid.uuid4(), is_active=True,
        )
        return user, site

    def test_two_users_never_cross(self, chroma_settings):
        user_a, site_a = self._tenant("a")
        user_b, site_b = self._tenant("b")
        _seed(site_a, texts=["Alpha corporation quarterly earnings brief."])
        _seed(site_b, texts=["Alpha corporation quarterly earnings brief."])

        hits = retrieve(user=user_a, website=site_a,
                        query="alpha corporation earnings")
        assert hits
        owners = {
            KnowledgeChunk.objects.get(id=h.chunk_id).user_id for h in hits
        }
        assert owners == {user_a.id}

    def test_poisoned_index_cannot_leak_foreign_chunks(self, chroma_settings):
        """Inject user B's chunk id directly into user A's collection -
        the strongest attack an index bug could mount. The Postgres
        re-fetch must drop it."""
        user_a, site_a = self._tenant("a")
        user_b, site_b = self._tenant("b")
        _seed(site_a, texts=["Public roadmap themes for the spring release."])
        _src_b, rows_b = _seed(
            site_b, texts=["SECRET: acquisition due diligence notes."])

        backend = vector_backends.get_backend()
        foreign = rows_b[0]
        # Plant the foreign chunk id, with its real embedding, inside
        # site A's collection under A's dimension.
        vec = foreign.embedding
        coll = backend._collection(site_a.id, len(vec))
        coll.add(ids=[str(foreign.id)], embeddings=[list(map(float, vec))],
                 metadatas=[{"source_id": "poison"}])

        hits = retrieve(user=user_a, website=site_a,
                        query="secret acquisition due diligence notes")
        leaked = [h for h in hits if h.chunk_id == str(foreign.id)]
        assert leaked == [], "poisoned index id crossed the tenant boundary"
        for h in hits:
            assert "SECRET" not in h.text

    def test_same_website_foreign_user_chunk_is_excluded(self, chroma_settings):
        """KnowledgeChunk carries its own user FK precisely so chunks are
        insulated per-user even within one website. The index is keyed by
        website alone, so this relies entirely on the re-fetch filter."""
        user_a, site_a = self._tenant("a")
        user_b, _site_b = self._tenant("b")
        _seed(site_a, texts=["Ordinary notes about onboarding emails."])

        src = KnowledgeSource.objects.create(
            user=user_b, website=site_a,
            url="https://a.example.com/foreign", kind=KnowledgeSource.KIND_DOCS,
            status=KnowledgeSource.STATUS_READY,
        )
        vectors, model, dim = embed_texts(["Foreign user note inside site A."])
        row = KnowledgeChunk.objects.create(
            source=src, user=user_b, website=site_a, chunk_index=0,
            text="Foreign user note inside site A.", embedding=vectors[0],
            embedding_model=model, embedding_dim=dim,
        )
        backend = vector_backends.get_backend()
        backend.replace_source(website_id=site_a.id, source_id=src.id,
                               chunk_ids=[row.id], vectors=vectors, dim=dim)

        hits = retrieve(user=user_a, website=site_a,
                        query="foreign user note inside site")
        assert all(h.chunk_id != str(row.id) for h in hits)
