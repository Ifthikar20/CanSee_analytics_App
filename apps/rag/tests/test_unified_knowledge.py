"""
Tests for the unified-knowledge-layer schema evolution (Phase 3A):

- KnowledgeSource.source_app / source_ref and KnowledgeChunk.metadata /
  recorded_at defaults (legacy rows keep working untouched).
- Retriever ``source_apps`` / ``since`` filters.
- Recency weighting: identical text scores lower the older its
  recorded_at (created_at fallback for legacy rows), except evergreen
  brand_input which keeps its raw cosine score.
- Context-block headers: synthetic-scheme sources are labeled by a
  human source_app label instead of the meaningless URL.
- ingest_url provenance passthrough.
- Migration sanity: models are fully captured by migration files.
"""
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.tests.factories import UserFactory
from apps.rag.models import KnowledgeChunk, KnowledgeSource
from apps.rag.services import ingest_service
from apps.rag.services.embedder import embed_one
from apps.rag.services.retriever import retrieve, retrieve_context_block
from apps.websites.tests.factories import WebsiteFactory


@pytest.fixture
def no_openai(settings):
    """Force the deterministic-fallback embedder so tests don't hit OpenAI."""
    settings.OPENAI_API_KEY = ""


@pytest.fixture
def tenant(db):
    return UserFactory(), WebsiteFactory()


def _make_source(user, website, url, *, source_app=None, source_ref="",
                 kind=KnowledgeSource.KIND_OTHER):
    kwargs = {}
    if source_app is not None:
        kwargs["source_app"] = source_app
    return KnowledgeSource.objects.create(
        user=user, website=website, url=url, kind=kind,
        status=KnowledgeSource.STATUS_READY, source_ref=source_ref,
        **kwargs,
    )


def _add_chunk(source, text, *, recorded_at=None, metadata=None, chunk_index=0):
    vec, model, dim = embed_one(text)
    return KnowledgeChunk.objects.create(
        source=source, user=source.user, website=source.website,
        chunk_index=chunk_index, text=text, embedding=vec,
        embedding_model=model, embedding_dim=dim,
        section_label="seed",
        metadata=metadata or {},
        recorded_at=recorded_at,
    )


@pytest.mark.django_db
class TestSchemaDefaults:
    def test_legacy_rows_get_evergreen_defaults(self, tenant):
        user, website = tenant
        source = KnowledgeSource.objects.create(
            user=user, website=website, url="https://example.com/legacy",
        )
        chunk = KnowledgeChunk.objects.create(
            source=source, user=user, website=website, text="legacy text",
        )
        assert source.source_app == KnowledgeSource.SOURCE_APP_BRAND_INPUT
        assert source.source_ref == ""
        assert chunk.metadata == {}
        assert chunk.recorded_at is None


@pytest.mark.django_db
class TestSourceAppsFilter:
    def test_filters_to_requested_apps(self, no_openai, tenant):
        user, website = tenant
        brand = _make_source(user, website, "https://example.com/about")
        _add_chunk(brand, "Cansee visibility platform overview")
        llm = _make_source(
            user, website, "llmres://audit-1",
            source_app=KnowledgeSource.SOURCE_APP_LLM_RESPONSE,
        )
        _add_chunk(llm, "Cansee visibility platform answer from Claude",
                   recorded_at=timezone.now())

        hits = retrieve(
            user=user, website=website, query="Cansee visibility platform",
            source_apps=[KnowledgeSource.SOURCE_APP_LLM_RESPONSE],
        )
        assert hits
        assert all(
            h.source_app == KnowledgeSource.SOURCE_APP_LLM_RESPONSE
            for h in hits
        )

        # No filter still sees both corpora.
        all_hits = retrieve(
            user=user, website=website, query="Cansee visibility platform",
        )
        assert {h.source_app for h in all_hits} == {
            KnowledgeSource.SOURCE_APP_BRAND_INPUT,
            KnowledgeSource.SOURCE_APP_LLM_RESPONSE,
        }


@pytest.mark.django_db
class TestSinceFilter:
    def test_since_drops_chunks_recorded_before_cutoff(self, no_openai, tenant):
        user, website = tenant
        now = timezone.now()
        source = _make_source(
            user, website, "llmres://audit-2",
            source_app=KnowledgeSource.SOURCE_APP_LLM_RESPONSE,
        )
        _add_chunk(source, "Cansee answer from last quarter",
                   recorded_at=now - timedelta(days=100), chunk_index=0)
        fresh = _add_chunk(source, "Cansee answer from this week",
                           recorded_at=now, chunk_index=1)

        hits = retrieve(
            user=user, website=website, query="Cansee answer",
            since=now - timedelta(days=7),
        )
        assert [h.chunk_id for h in hits] == [str(fresh.id)]

    def test_since_falls_back_to_created_at_for_legacy_chunks(
        self, no_openai, tenant,
    ):
        user, website = tenant
        source = _make_source(user, website, "https://example.com/page")
        legacy = _add_chunk(source, "Cansee legacy brand copy")  # recorded_at None

        included = retrieve(
            user=user, website=website, query="Cansee legacy brand copy",
            since=timezone.now() - timedelta(days=1),
        )
        assert [h.chunk_id for h in included] == [str(legacy.id)]

        excluded = retrieve(
            user=user, website=website, query="Cansee legacy brand copy",
            since=timezone.now() + timedelta(days=1),
        )
        assert excluded == []


@pytest.mark.django_db
class TestRecencyWeighting:
    QUERY = "Cansee mentioned as the best analytics platform"

    def test_older_identical_chunk_scores_lower(self, no_openai, tenant):
        user, website = tenant
        now = timezone.now()
        source = _make_source(
            user, website, "llmres://audit-3",
            source_app=KnowledgeSource.SOURCE_APP_LLM_RESPONSE,
        )
        old = _add_chunk(source, self.QUERY,
                         recorded_at=now - timedelta(days=150), chunk_index=0)
        fresh = _add_chunk(source, self.QUERY,
                           recorded_at=now, chunk_index=1)

        hits = retrieve(user=user, website=website, query=self.QUERY)
        assert [h.chunk_id for h in hits] == [str(fresh.id), str(old.id)]
        assert hits[0].score > hits[1].score

    def test_brand_input_is_evergreen(self, no_openai, tenant):
        user, website = tenant
        now = timezone.now()
        source = _make_source(user, website, "https://example.com/evergreen")
        _add_chunk(source, self.QUERY,
                   recorded_at=now - timedelta(days=150), chunk_index=0)
        _add_chunk(source, self.QUERY, recorded_at=now, chunk_index=1)

        hits = retrieve(user=user, website=website, query=self.QUERY)
        assert len(hits) == 2
        # No decay applied: identical text keeps identical (raw cosine) score.
        assert abs(hits[0].score - hits[1].score) < 1e-9

    def test_legacy_chunk_decays_via_created_at_fallback(
        self, no_openai, tenant,
    ):
        user, website = tenant
        source = _make_source(
            user, website, "secalert://scan-1",
            source_app=KnowledgeSource.SOURCE_APP_SECURITY_ALERT,
        )
        stale = _add_chunk(source, self.QUERY, chunk_index=0)  # recorded_at None
        # Backdate the ingest time; .update() bypasses auto_now_add.
        KnowledgeChunk.objects.filter(id=stale.id).update(
            created_at=timezone.now() - timedelta(days=150),
        )
        fresh = _add_chunk(source, self.QUERY, chunk_index=1)

        hits = retrieve(user=user, website=website, query=self.QUERY)
        assert [h.chunk_id for h in hits] == [str(fresh.id), str(stale.id)]
        assert hits[0].score > hits[1].score


@pytest.mark.django_db
class TestContextBlockLabels:
    @pytest.mark.parametrize("source_app,label", [
        (KnowledgeSource.SOURCE_APP_LLM_RESPONSE, "AI answers"),
        (KnowledgeSource.SOURCE_APP_SECURITY_ALERT, "Security findings"),
        (KnowledgeSource.SOURCE_APP_AGENT_INSIGHT, "Agent insights"),
        (KnowledgeSource.SOURCE_APP_SEARCH_QUERIES, "Search queries"),
        (KnowledgeSource.SOURCE_APP_PROMPT_NOTES, "Prompt notes"),
    ])
    def test_synthetic_scheme_labeled_by_source_app(
        self, no_openai, tenant, source_app, label,
    ):
        user, website = tenant
        source = _make_source(
            user, website, f"syn://{source_app}/1", source_app=source_app,
        )
        _add_chunk(source, "Cansee weekly visibility insight",
                   recorded_at=timezone.now())
        block = retrieve_context_block(
            user=user, website=website, query="Cansee weekly visibility insight",
        )
        assert f"({label})" in block
        assert "syn://" not in block

    def test_real_url_still_shown(self, no_openai, tenant):
        user, website = tenant
        source = _make_source(user, website, "https://example.com/product")
        _add_chunk(source, "Cansee product page copy")
        block = retrieve_context_block(
            user=user, website=website, query="Cansee product page copy",
        )
        assert "https://example.com/product" in block

    def test_synthetic_scheme_without_named_label_humanizes(
        self, no_openai, tenant,
    ):
        # Legacy audit:// sources carry the default source_app.
        user, website = tenant
        source = _make_source(
            user, website, "audit://abc-123",
            kind=KnowledgeSource.KIND_AUDIT_CONTEXT,
        )
        _add_chunk(source, "Cansee audit context intel")
        block = retrieve_context_block(
            user=user, website=website, query="Cansee audit context intel",
        )
        assert "(Brand input)" in block
        assert "audit://" not in block


@pytest.mark.django_db
class TestIngestPassthrough:
    def test_provenance_lands_on_source_and_chunks(self, no_openai, tenant):
        user, website = tenant
        stamp = timezone.now() - timedelta(days=3)
        meta = {"provider": "claude", "audit_id": "audit-9", "is_mentioned": True}
        result = ingest_service.ingest_url(
            user=user, website=website, url="llmres://audit-9",
            title="Audit 9 responses",
            text="Claude called Cansee the leading analytics platform. " * 40,
            source_app=KnowledgeSource.SOURCE_APP_LLM_RESPONSE,
            source_ref="audit-9",
            metadata=meta,
            recorded_at=stamp,
        )
        assert result.status == KnowledgeSource.STATUS_READY
        source = KnowledgeSource.objects.get(id=result.source_id)
        assert source.source_app == KnowledgeSource.SOURCE_APP_LLM_RESPONSE
        assert source.source_ref == "audit-9"
        chunks = list(KnowledgeChunk.objects.filter(source=source))
        assert chunks
        assert all(c.metadata == meta for c in chunks)
        assert all(c.recorded_at == stamp for c in chunks)

    def test_defaults_preserve_brand_input_behavior(self, no_openai, tenant):
        user, website = tenant
        result = ingest_service.ingest_url(
            user=user, website=website, url="https://example.com",
            text="Plain brand-input ingest without provenance. " * 40,
        )
        assert result.status == KnowledgeSource.STATUS_READY
        source = KnowledgeSource.objects.get(id=result.source_id)
        assert source.source_app == KnowledgeSource.SOURCE_APP_BRAND_INPUT
        assert source.source_ref == ""
        chunk = KnowledgeChunk.objects.filter(source=source).first()
        assert chunk.metadata == {}
        assert chunk.recorded_at is None

    def test_reingest_updates_provenance(self, no_openai, tenant):
        user, website = tenant
        first_stamp = timezone.now() - timedelta(days=30)
        ingest_service.ingest_url(
            user=user, website=website, url="secalert://w1/key",
            text="High severity hallucination finding about Cansee. " * 40,
            source_app=KnowledgeSource.SOURCE_APP_SECURITY_ALERT,
            source_ref="dedupe-v1",
            recorded_at=first_stamp,
        )
        second_stamp = timezone.now()
        ingest_service.ingest_url(
            user=user, website=website, url="secalert://w1/key",
            text="Updated severity finding about Cansee resolved. " * 40,
            source_app=KnowledgeSource.SOURCE_APP_SECURITY_ALERT,
            source_ref="dedupe-v2",
            recorded_at=second_stamp,
        )
        source = KnowledgeSource.objects.get(
            user=user, website=website, url="secalert://w1/key",
        )
        assert source.source_ref == "dedupe-v2"
        # Chunks were rebuilt with the new recorded_at, not duplicated.
        stamps = set(
            KnowledgeChunk.objects.filter(source=source)
            .values_list("recorded_at", flat=True)
        )
        assert stamps == {second_stamp}


class TestMigrationSanity:
    def test_models_fully_captured_by_migrations(self):
        from django.apps import apps as django_apps
        from django.db.migrations.autodetector import MigrationAutodetector
        from django.db.migrations.loader import MigrationLoader
        from django.db.migrations.state import ProjectState

        loader = MigrationLoader(None, ignore_no_migrations=True)
        autodetector = MigrationAutodetector(
            loader.project_state(),
            ProjectState.from_apps(django_apps),
        )
        changes = autodetector.changes(graph=loader.graph)
        assert "rag" not in changes, (
            "rag model changes are missing a migration; run "
            "manage.py makemigrations rag"
        )
