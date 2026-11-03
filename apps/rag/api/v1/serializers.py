from urllib.parse import urlparse

from rest_framework import serializers

from apps.rag.models import KnowledgeChunk, KnowledgeSource
from apps.rag.services.embedder import is_model_vector


class KnowledgeSourceSerializer(serializers.ModelSerializer):
    kind_display = serializers.CharField(source="get_kind_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    is_paste = serializers.SerializerMethodField()
    domain = serializers.SerializerMethodField()

    class Meta:
        model = KnowledgeSource
        fields = [
            "id", "url", "title", "kind", "kind_display",
            "status", "status_display", "chunk_count",
            "error_message", "last_ingested_at", "created_at", "updated_at",
            "is_paste", "domain",
        ]
        read_only_fields = [
            "id", "kind_display", "status", "status_display",
            "chunk_count", "error_message", "last_ingested_at",
            "created_at", "updated_at", "is_paste", "domain",
        ]

    def get_is_paste(self, obj) -> bool:
        return obj.url.startswith("paste://")

    def get_domain(self, obj) -> str:
        """Host portion of the source URL, or ``"paste"`` for uploaded text.

        The frontend groups the source list by this field so a 50-page
        crawl of the same site collapses into a single expandable node.
        """
        if obj.url.startswith("paste://"):
            return "paste"
        try:
            host = urlparse(obj.url).hostname or ""
        except ValueError:
            host = ""
        return host or "other"


class IngestURLSerializer(serializers.Serializer):
    url = serializers.URLField(max_length=2000)
    kind = serializers.ChoiceField(
        choices=[c[0] for c in KnowledgeSource.KIND_CHOICES],
        required=False, default=KnowledgeSource.KIND_OTHER,
    )
    title = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")
    crawl = serializers.BooleanField(required=False, default=False)
    page_cap = serializers.IntegerField(required=False, default=12, min_value=1, max_value=50)
    depth = serializers.IntegerField(required=False, default=1, min_value=0, max_value=2)

    def validate_url(self, value):
        """SSRF guard — reject loopback / private / metadata addresses."""
        from core.validators.url_safety import UnsafeURLError, assert_url_safe
        try:
            return assert_url_safe(value)
        except UnsafeURLError as exc:
            raise serializers.ValidationError(exc.messages) from exc


class UploadTextSerializer(serializers.Serializer):
    """Paste-in ingest: raw text or markdown, no URL required."""

    title = serializers.CharField(max_length=500)
    kind = serializers.ChoiceField(
        choices=[c[0] for c in KnowledgeSource.KIND_CHOICES],
        required=False, default=KnowledgeSource.KIND_OTHER,
    )
    text = serializers.CharField(min_length=20, max_length=200_000)


class RetrieveSerializer(serializers.Serializer):
    query = serializers.CharField(max_length=2000)
    top_k = serializers.IntegerField(required=False, default=5, min_value=1, max_value=20)
    kinds = serializers.ListField(
        child=serializers.ChoiceField(choices=[c[0] for c in KnowledgeSource.KIND_CHOICES]),
        required=False, default=list,
    )
    # Optional: narrow retrieval to a specific source. Powers the
    # "test this source" input in the Brand Input detail drawer.
    source_id = serializers.UUIDField(required=False, allow_null=True)


class HitSerializer(serializers.Serializer):
    chunk_id = serializers.CharField()
    source_id = serializers.CharField()
    source_url = serializers.CharField()
    source_kind = serializers.CharField()
    section_label = serializers.CharField()
    text = serializers.CharField()
    score = serializers.FloatField()


class KnowledgeChunkSerializer(serializers.ModelSerializer):
    source_url = serializers.CharField(source="source.url", read_only=True)
    # False while the chunk has no model vector, so search cannot reach it.
    embedded = serializers.SerializerMethodField()

    class Meta:
        model = KnowledgeChunk
        fields = [
            "id", "source_id", "source_url", "chunk_index",
            "section_label", "text", "embedding_model", "embedding_dim",
            "embedded", "token_count", "created_at",
        ]
        read_only_fields = fields

    def get_embedded(self, obj) -> bool:
        return is_model_vector(obj.embedding_model)
