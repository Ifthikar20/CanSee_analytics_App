from rest_framework import serializers

from apps.llm_ranking.models import LLMRankingAudit, LLMRankingResult, LLMRankingSchedule


class LLMRankingResultSerializer(serializers.ModelSerializer):
    provider_display = serializers.CharField(source="get_provider_display", read_only=True)
    sentiment_display = serializers.CharField(source="get_sentiment_display", read_only=True)
    prompt_type = serializers.SerializerMethodField()
    prompt_type_display = serializers.SerializerMethodField()

    class Meta:  # type: ignore[override]
        model = LLMRankingResult
        fields = [
            "id", "provider", "provider_display", "prompt", "prompt_type", "prompt_type_display",
            "response_text", "is_mentioned", "mention_rank",
            "sentiment", "sentiment_display", "confidence_score",
            "mention_context", "query_succeeded", "error_message",
            "run_id", "is_linked", "competitors_mentioned",
            "primary_recommendation", "citations", "citation_countries",
            "extraction_model", "extraction_version",
            "prompt_source_label",
        ]
        read_only_fields = fields

    def get_prompt_type(self, obj):
        try:
            return obj.prompt_type or "custom"
        except Exception:
            return "custom"

    def get_prompt_type_display(self, obj):
        try:
            return obj.get_prompt_type_display()
        except Exception:
            return "Custom"


class LLMRankingAuditSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    extraction_method_display = serializers.CharField(
        source="get_extraction_method_display", read_only=True
    )
    results = LLMRankingResultSerializer(many=True, read_only=True)

    class Meta:  # type: ignore[override]
        model = LLMRankingAudit
        fields = [
            "id", "status", "status_display",
            "business_name", "business_description", "industry", "location", "keywords",
            "region", "prompt_source", "prompts", "overall_score", "mention_rate", "mention_rate_smoothed",
            "avg_mention_rank", "brand_strengths", "citation_countries",
            "mention_rate_ci_lower", "mention_rate_ci_upper",
            "runs_per_query", "extraction_method", "extraction_method_display",
            "providers_queried", "queries_completed", "total_queries",
            "total_tokens", "total_cost_usd",
            "started_at", "completed_at", "created_at",
            "results",
        ]
        read_only_fields = [
            "id", "status", "status_display", "overall_score", "mention_rate",
            "mention_rate_smoothed", "avg_mention_rank", "brand_strengths",
            "citation_countries",
            "mention_rate_ci_lower", "mention_rate_ci_upper",
            "extraction_method", "extraction_method_display",
            "providers_queried", "queries_completed", "total_queries",
            "total_tokens", "total_cost_usd",
            "started_at", "completed_at", "created_at",
            "results",
        ]


class LLMRankingAuditListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for list view — includes prompts for expandable rows."""
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:  # type: ignore[override]
        model = LLMRankingAudit
        fields = [
            "id", "status", "status_display",
            "business_name", "industry", "location", "region", "prompt_source",
            "overall_score", "mention_rate", "mention_rate_smoothed",
            "avg_mention_rank", "brand_strengths", "citation_countries",
            "mention_rate_ci_lower", "mention_rate_ci_upper",
            "providers_queried", "queries_completed", "total_queries",
            "total_tokens", "total_cost_usd",
            "prompts", "context_urls",
            "started_at", "completed_at", "created_at",
        ]
        read_only_fields = fields


class RunAuditSerializer(serializers.Serializer):
    """
    Input serializer for creating a new audit.

    business_name/industry/description/keywords are OPTIONAL overrides — when
    omitted the view reads them from the Website row. This prevents clients
    from having to re-send data the server already knows, and keeps audits
    consistent with the tracked website.
    """
    business_name = serializers.CharField(max_length=200, required=False, allow_blank=True)
    business_description = serializers.CharField(required=False, default="", allow_blank=True)
    industry = serializers.CharField(max_length=100, required=False, allow_blank=True)
    location = serializers.CharField(max_length=200, required=False, default="", allow_blank=True)
    keywords = serializers.ListField(
        child=serializers.CharField(max_length=100),
        required=False,
        default=list,
        max_length=20,
    )
    # Geographic region for the audit. Drives prompt flavour, Perplexity
    # web-search location, and citation country attribution. Default is
    # global (no geo bias).
    region = serializers.ChoiceField(
        choices=[
            "global", "us", "ca", "in", "uk", "de", "au",
        ],
        required=False, default="global",
    )
    # Optionally supply custom prompts instead of running the saved list
    custom_prompts = serializers.ListField(
        child=serializers.CharField(max_length=500),
        required=False,
        default=list,
        max_length=10,
    )
    # Which providers to query (defaults to all configured)
    providers = serializers.ListField(
        child=serializers.ChoiceField(choices=[
            "claude", "gpt4", "gemini", "perplexity",
            "meta_llama", "mistral", "cohere", "deepseek",
            "grok", "amazon_nova",
        ]),
        required=False,
        default=list,
    )
    # Extra URLs for content enrichment (blogs, product pages, etc.)
    # Each entry is sanitised via the SSRF guard so a user cannot smuggle
    # in a fetch of internal services through the audit pipeline.
    context_urls = serializers.ListField(
        child=serializers.URLField(max_length=500),
        required=False,
        default=list,
        max_length=5,
    )

    def validate_context_urls(self, urls):
        from core.validators.url_safety import UnsafeURLError, assert_url_safe
        cleaned = []
        for u in urls or []:
            try:
                cleaned.append(assert_url_safe(u))
            except UnsafeURLError as exc:
                raise serializers.ValidationError(
                    f"context_url '{u}' rejected: {exc}",
                ) from exc
        return cleaned


class LLMRankingScheduleSerializer(serializers.ModelSerializer):
    frequency_display = serializers.CharField(source="get_frequency_display", read_only=True)

    class Meta:  # type: ignore[override]
        model = LLMRankingSchedule
        fields = [
            "id", "is_enabled", "frequency", "frequency_display",
            "business_name", "business_description", "industry", "location",
            "keywords", "providers",
            "next_run_at", "last_run_at",
            "consecutive_failures", "last_failure_at", "auto_pause_threshold",
            "created_at",
        ]
        read_only_fields = [
            "id", "next_run_at", "last_run_at",
            "consecutive_failures", "last_failure_at",
            "created_at",
        ]


class CreateScheduleSerializer(serializers.Serializer):
    """Input serializer for creating/updating a periodic schedule."""
    is_enabled = serializers.BooleanField(default=True)
    frequency = serializers.ChoiceField(
        choices=["weekly", "biweekly", "monthly"], default="weekly"
    )
    business_name = serializers.CharField(max_length=200)
    business_description = serializers.CharField(required=False, default="", allow_blank=True)
    industry = serializers.CharField(max_length=100)
    location = serializers.CharField(max_length=200, required=False, default="", allow_blank=True)
    keywords = serializers.ListField(
        child=serializers.CharField(max_length=100),
        required=False,
        default=list,
        max_length=20,
    )
    providers = serializers.ListField(
        child=serializers.ChoiceField(choices=["claude", "gpt4", "gemini", "perplexity"]),
        required=False,
        default=list,
    )

