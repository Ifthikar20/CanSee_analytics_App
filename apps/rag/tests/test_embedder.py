"""Tests for the deterministic-fallback embedder + similarity math."""
from apps.rag.services.embedder import (
    FALLBACK_DIM,
    FALLBACK_MODEL,
    cosine_similarity,
    embed_one,
    embed_texts,
)


class TestHashEmbedFallback:
    def test_empty_input_returns_empty(self):
        vectors, model, dim = embed_texts([])
        assert vectors == []
        assert dim == 0

    def test_dim_matches_fallback_when_no_api_key(self, settings):
        settings.OPENAI_API_KEY = ""
        vec, model, dim = embed_one("analytics platform with dashboards")
        assert model == FALLBACK_MODEL
        assert dim == FALLBACK_DIM
        assert len(vec) == FALLBACK_DIM
        assert any(v != 0 for v in vec)

    def test_identical_text_gives_high_similarity(self, settings):
        settings.OPENAI_API_KEY = ""
        a, _, _ = embed_one("Cansee analytics for SaaS teams")
        b, _, _ = embed_one("Cansee analytics for SaaS teams")
        assert cosine_similarity(a, b) > 0.99

    def test_unrelated_text_gives_low_similarity(self, settings):
        settings.OPENAI_API_KEY = ""
        a, _, _ = embed_one("retail point-of-sale terminal")
        b, _, _ = embed_one("Cansee analytics for SaaS teams")
        assert cosine_similarity(a, b) < 0.5


class TestCosineSimilarity:
    def test_orthogonal_vectors_zero(self):
        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0

    def test_identical_vectors_one(self):
        assert abs(cosine_similarity([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) - 1.0) < 1e-9

    def test_mismatched_dim_returns_zero(self):
        assert cosine_similarity([1.0, 2.0], [1.0, 2.0, 3.0]) == 0.0

    def test_empty_returns_zero(self):
        assert cosine_similarity([], [1.0]) == 0.0
