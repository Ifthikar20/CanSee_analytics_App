"""DRF serializers for the brand_vault API."""
from rest_framework import serializers

from apps.brand_vault.models import (
    BrandFact,
    BrandSecurityConfig,
    FactRevision,
    SafetyAlert,
)


class FactRevisionSerializer(serializers.ModelSerializer):
    actor_email = serializers.SerializerMethodField()

    class Meta:
        model = FactRevision
        fields = (
            "id", "action", "before", "after",
            "actor_user", "actor_email", "created_at",
        )
        read_only_fields = fields

    def get_actor_email(self, obj):
        return getattr(obj.actor_user, "email", "") or ""


class BrandFactSerializer(serializers.ModelSerializer):
    is_current = serializers.SerializerMethodField()

    class Meta:
        model = BrandFact
        fields = (
            "id", "website",
            "subject", "predicate", "object",
            "source_chunk", "source_url", "extracted_by",
            "confidence", "status",
            "version_from", "version_to", "superseded_by",
            "product_line", "topic", "audience",
            "is_current",
            "created_at", "updated_at",
        )
        read_only_fields = fields

    def get_is_current(self, obj):
        return obj.version_to is None


class BrandFactDetailSerializer(BrandFactSerializer):
    revisions = FactRevisionSerializer(many=True, read_only=True)

    class Meta(BrandFactSerializer.Meta):
        fields = BrandFactSerializer.Meta.fields + ("revisions",)
        read_only_fields = fields


class BrandFactEditSerializer(serializers.Serializer):
    subject = serializers.CharField(max_length=300)
    predicate = serializers.CharField(max_length=200)
    object = serializers.CharField()


class SafetyAlertSerializer(serializers.ModelSerializer):
    """Alert payload for the alert-center UI.

    Span contract: ``evidence_spans`` offsets are Unicode-codepoint
    indices into ``snippet`` exactly as serialized here. Each span echoes
    its ``text`` so the renderer can verify the slice and re-anchor by
    searching for the echo if the offsets ever drift (e.g. UTF-16
    surrogate pairs on the JS side).

    ``detector`` and ``recommended_action`` are derived from the detector
    registry at serialization time — legacy rows without a detector code
    fall back via their issue, and copy edits in the registry apply
    everywhere without a migration.
    """

    detector = serializers.SerializerMethodField()
    recommended_action = serializers.SerializerMethodField()
    result_public_id = serializers.SerializerMethodField()
    source_prompt = serializers.SerializerMethodField()
    result_context = serializers.SerializerMethodField()

    class Meta:
        model = SafetyAlert
        fields = (
            "id", "website", "prompt",
            # The audit response this finding was raised from, when it came
            # from the response auditor. Lets the prompt detail page show
            # findings against the exact answer that produced them.
            "result", "result_public_id", "source_prompt",
            "reference", "detector_code", "detector",
            "agent_id", "source", "source_url", "title", "sentiment_score",
            "model", "prompt_text", "snippet", "evidence_spans",
            "issue", "detail", "severity", "status",
            "detected_at", "resolved_at",
            "first_seen_at", "last_seen_at", "occurrence_count",
            "recommended_action", "result_context",
            "evidence_chunks",
            "created_at",
        )
        read_only_fields = fields

    def _detector_for(self, obj):
        from apps.brand_vault.services.security.detectors import (
            DETECTOR_INDEX,
            ISSUE_FALLBACK,
        )
        code = obj.detector_code or ISSUE_FALLBACK.get(obj.issue, "")
        return DETECTOR_INDEX.get(code)

    def get_detector(self, obj):
        detector = self._detector_for(obj)
        if detector is None:
            return None
        from apps.brand_vault.services.security.detectors import CATEGORIES
        category_display = dict(CATEGORIES).get(detector.category, detector.category)
        return {
            "code": detector.code,
            "display_name": detector.display_name,
            "category": detector.category,
            "category_display": category_display,
        }

    def get_recommended_action(self, obj):
        detector = self._detector_for(obj)
        return detector.recommended_action if detector else ""

    def get_result_public_id(self, obj):
        result = obj.result
        return str(result.public_id) if result and result.public_id else None

    def get_source_prompt(self, obj):
        result = obj.result
        if result and result.source_prompt_id:
            return str(result.source_prompt_id)
        return None

    def get_result_context(self, obj):
        """Structured picture of the answer the finding came from: where
        the brand ranks, who else is named, and what the answer actually
        recommends. This is what lets the detail drawer SHOW the situation
        ("position 8 of 15, top recommendation: X") instead of only
        describing it in prose."""
        result = obj.result
        if result is None:
            return None
        competitors = []
        for c in (result.competitors_mentioned or []):
            if isinstance(c, dict) and (c.get("name") or "").strip():
                competitors.append({
                    "name": (c.get("name") or "").strip(),
                    "position": c.get("position") if isinstance(c.get("position"), int) else None,
                    "sentiment": c.get("sentiment") or "",
                })
        total_named = len(competitors) + (1 if result.is_mentioned else 0)
        return {
            "is_mentioned": result.is_mentioned,
            "sentiment": result.sentiment or "",
            "mention_rank": result.mention_rank,
            "brands_total": total_named or None,
            "competitors": competitors[:15],
            "competitors_truncated": max(0, len(competitors) - 15),
            "primary_recommendation": result.primary_recommendation or "",
        }


class BrandSecurityConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = BrandSecurityConfig
        fields = (
            "id", "website", "brand_terms",
            "llm_judge_enabled", "last_response_scan_at",
            "created_at", "updated_at",
        )
        read_only_fields = (
            "id", "website", "last_response_scan_at", "created_at", "updated_at",
        )


class FactImportItemSerializer(serializers.Serializer):
    subject = serializers.CharField(max_length=300)
    predicate = serializers.CharField(max_length=200)
    object = serializers.CharField()
    product_line = serializers.CharField(max_length=120, required=False, allow_blank=True)
    topic = serializers.CharField(max_length=120, required=False, allow_blank=True)
    confidence = serializers.FloatField(required=False, default=0.9)
    source_url = serializers.URLField(required=False, allow_blank=True, max_length=1000)
