"""DRF serializers for the prompt_library REST surface."""
from __future__ import annotations

from rest_framework import serializers

from apps.prompt_library.models import (
    BrandPrompt,
    Industry,
    Prompt,
    PromptSampleEntry,
    PromptSampleRun,
    PromptSchedule,
    PromptVariableSet,
)


class IndustrySerializer(serializers.ModelSerializer):
    class Meta:
        model = Industry
        fields = ("id", "slug", "name", "description")


class PromptSerializer(serializers.ModelSerializer):
    industry_slug = serializers.CharField(source="industry.slug", read_only=True)
    source_label = serializers.CharField(source="get_source_display", read_only=True)
    style_label = serializers.CharField(source="get_style_display", read_only=True)
    # No demand signal is collected. The stored column ranks prompts by
    # source type, recency and paraphrase count, so it is exposed as
    # ``source_score``; ``demand_score`` stays in the payload as null.
    demand_score = serializers.SerializerMethodField()
    source_score = serializers.FloatField(source="demand_score", read_only=True)

    class Meta:
        model = Prompt
        fields = (
            "id",
            "industry",
            "industry_slug",
            "text",
            "excerpt",
            "template_text",
            "template_variables",
            "intent_bucket",
            "language",
            "source",
            "source_label",
            "source_url",
            "style",
            "style_label",
            "demand_score",
            "source_score",
            "effectiveness_score",
            "effectiveness_components",
            "runs_count",
            "last_used_at",
            "is_active",
            "created_at",
        )
        read_only_fields = (
            "effectiveness_score",
            "effectiveness_components",
            "runs_count",
            "last_used_at",
            "created_at",
            "industry_slug",
            "source_label",
            "style_label",
            "template_variables",
        )

    def get_demand_score(self, obj) -> None:
        return None


class PromptCreateSerializer(serializers.Serializer):
    """Payload for the per-website "new prompt" endpoint."""

    industry_id = serializers.UUIDField(required=False)
    template_text = serializers.CharField(required=False, allow_blank=True)
    intent_bucket = serializers.CharField(required=False, default="category")
    style = serializers.CharField(required=False, default="question")
    text = serializers.CharField(required=False, allow_blank=True)
    # Topic == prompt bundle (Industry). When provided as a name, the view
    # resolves or creates the matching Industry and files the prompt under it.
    topic = serializers.CharField(required=False, allow_blank=True)
    # Classification chips and the scan location (ISO-2). Applied to every
    # prompt created in this request.
    tags = serializers.ListField(
        child=serializers.CharField(), required=False, default=list,
    )
    location = serializers.CharField(required=False, allow_blank=True)
    # Model variants ("<provider>:<model_id>") every prompt in this request
    # is scanned with. Empty == every configured provider's default model.
    models = serializers.ListField(
        child=serializers.CharField(), required=False, default=list,
    )
    # False tracks the prompts without scanning them now (Ask AI's Track
    # already has its answers; Autopilot asks it from its next run).
    scan = serializers.BooleanField(required=False, default=True)

    def validate_models(self, value):
        from apps.llm_ranking.providers import list_model_variants
        valid = {v["id"] for v in list_model_variants()}
        cleaned: list[str] = []
        seen: set[str] = set()
        for vid in value or []:
            vid = (vid or "").strip()
            if not vid or vid in seen:
                continue
            if vid not in valid:
                raise serializers.ValidationError(f"Unknown model variant: {vid}")
            seen.add(vid)
            cleaned.append(vid)
        return cleaned

    def validate(self, attrs):
        if not (attrs.get("template_text") or "").strip() and not (attrs.get("text") or "").strip():
            raise serializers.ValidationError({"text": "Provide the prompt text."})
        return attrs


class PreviewSampleRequestSerializer(serializers.Serializer):
    industry_id = serializers.UUIDField()
    n = serializers.IntegerField(min_value=1, max_value=200, default=20)
    intent_mix = serializers.DictField(child=serializers.IntegerField(min_value=0), required=False)
    strategy = serializers.ChoiceField(
        choices=["stratified", "recent", "top_demand"], default="stratified"
    )


class UseLibrarySampleRequestSerializer(serializers.Serializer):
    industry_id = serializers.UUIDField()
    n = serializers.IntegerField(min_value=1, max_value=200, default=50)
    strategy = serializers.ChoiceField(
        choices=["stratified", "recent", "top_demand"], default="stratified"
    )
    seed = serializers.IntegerField(required=False)


class PromptSampleEntrySerializer(serializers.ModelSerializer):
    prompt = PromptSerializer(read_only=True)

    class Meta:
        model = PromptSampleEntry
        fields = ("rank", "prompt")


class PromptSampleRunSerializer(serializers.ModelSerializer):
    entries = serializers.SerializerMethodField()
    industry = IndustrySerializer(read_only=True)

    class Meta:
        model = PromptSampleRun
        fields = ("id", "industry", "seed", "strategy", "entries", "created_at")

    def get_entries(self, obj):
        qs = (
            PromptSampleEntry.objects.filter(sample_run=obj)
            .select_related("prompt", "prompt__industry")
            .order_by("rank")
        )
        return PromptSampleEntrySerializer(qs, many=True).data


class BrandPromptSerializer(serializers.ModelSerializer):
    prompt = PromptSerializer(read_only=True)
    prompt_id = serializers.UUIDField(write_only=True)

    class Meta:
        model = BrandPrompt
        fields = ("id", "website", "prompt", "prompt_id", "notes", "is_archived", "created_at")
        read_only_fields = ("id", "website", "created_at")


class PromptVariableSetSerializer(serializers.ModelSerializer):
    class Meta:
        model = PromptVariableSet
        fields = ("variables", "updated_at")


class AutoTemplateRequestSerializer(serializers.Serializer):
    raw_text = serializers.CharField()


class SmokeTestRequestSerializer(serializers.Serializer):
    provider = serializers.ChoiceField(
        choices=["claude", "gpt4", "gemini", "perplexity"], default="claude",
    )
    website_id = serializers.UUIDField()


class SynthesizeRequestSerializer(serializers.Serializer):
    industry_id = serializers.UUIDField()
    count = serializers.IntegerField(min_value=1, max_value=50, default=10)
    style = serializers.ChoiceField(
        choices=["question", "story", "comparison", "local", "how_to", "listicle"],
        default="question",
    )
    examples = serializers.ListField(
        child=serializers.CharField(), required=False, default=list,
    )


class VariableSetUpdateSerializer(serializers.Serializer):
    variables = serializers.DictField(child=serializers.CharField(allow_blank=True))


class GenerateFromContextRequestSerializer(serializers.Serializer):
    """Validate the free-form context input for prompt generation."""

    context = serializers.CharField()
    # Cap at 50 — the LLM generator gets unreliable past that, and
    # each prompt also represents a future audit cost downstream so
    # we don't want users requesting hundreds at a time.
    count = serializers.IntegerField(min_value=1, max_value=50, default=20)
    persist = serializers.BooleanField(default=False)
    website_id = serializers.UUIDField(required=False)

    def validate_context(self, value: str) -> str:
        cleaned = (value or "").strip()
        words = [w for w in cleaned.split() if w]
        if len(words) < 5:
            raise serializers.ValidationError(
                "Context must contain at least 5 words.",
            )
        return cleaned


class GenerateRelatedRequestSerializer(serializers.Serializer):
    """Validate the seed prompt for related-prompt recommendations.

    Unlike :class:`GenerateFromContextRequestSerializer` there is no word
    minimum: the seed is a single existing prompt the service wraps into a
    richer instruction before calling the AI model.
    """

    prompt = serializers.CharField()
    # Small by design — this is a secondary recommendation, not a bulk
    # generation, so we cap it low to keep DeepSeek token spend minimal.
    count = serializers.IntegerField(min_value=1, max_value=10, default=5)

    def validate_prompt(self, value: str) -> str:
        cleaned = (value or "").strip()
        if len(cleaned) < 3:
            raise serializers.ValidationError("Prompt is too short.")
        return cleaned


class PromptScheduleSerializer(serializers.ModelSerializer):
    """Read serializer for a per-prompt run schedule."""

    frequency_display = serializers.CharField(
        source="get_frequency_display", read_only=True,
    )
    is_paused = serializers.SerializerMethodField()

    class Meta:
        model = PromptSchedule
        fields = [
            "id", "is_enabled", "frequency", "frequency_display",
            "next_run_at", "last_run_at",
            "consecutive_failures", "last_failure_at", "auto_pause_threshold",
            "is_paused", "created_at",
        ]
        read_only_fields = [
            "id", "next_run_at", "last_run_at",
            "consecutive_failures", "last_failure_at", "auto_pause_threshold",
            "created_at",
        ]

    def get_is_paused(self, obj) -> bool:
        # Distinguishes an auto-paused schedule (too many failures) from
        # one the user simply turned off.
        return (not obj.is_enabled) and (obj.consecutive_failures or 0) >= (
            obj.auto_pause_threshold or 3
        )


class CreatePromptScheduleSerializer(serializers.Serializer):
    """Input serializer for creating/updating a per-prompt schedule."""

    is_enabled = serializers.BooleanField(default=True)
    frequency = serializers.ChoiceField(
        choices=["daily", "weekly", "monthly"], default="weekly",
    )
