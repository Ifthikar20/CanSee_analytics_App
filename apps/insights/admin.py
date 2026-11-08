from django.contrib import admin

from apps.insights.models import (
    AnswerAnalysis,
    AnswerClaim,
    AnswerTheme,
    EvidencePage,
    FanoutGoogleCheck,
    InsightRun,
    PromptIdea,
    PromptSuggestion,
    SerpFeatureCapture,
    ShoppingResult,
    SiteAudit,
    SiteAuditPage,
    TrackedProduct,
)


@admin.register(InsightRun)
class InsightRunAdmin(admin.ModelAdmin):
    list_display = ("website", "kind", "status", "units_total", "units_done", "created_at")
    list_filter = ("kind", "status")
    raw_id_fields = ("website", "created_by")
    readonly_fields = ("created_at", "started_at", "finished_at", "heartbeat_at")


@admin.register(AnswerAnalysis)
class AnswerAnalysisAdmin(admin.ModelAdmin):
    list_display = ("website", "engine", "status", "claims_count", "themes_count", "answered_at")
    list_filter = ("status", "engine", "version")
    raw_id_fields = ("website", "result", "run", "prompt")


@admin.register(AnswerClaim)
class AnswerClaimAdmin(admin.ModelAdmin):
    list_display = ("website", "engine", "topic", "verdict", "answered_at")
    list_filter = ("verdict", "topic")
    search_fields = ("text",)
    raw_id_fields = ("analysis", "website", "prompt", "evidence_fact")


@admin.register(AnswerTheme)
class AnswerThemeAdmin(admin.ModelAdmin):
    list_display = ("website", "engine", "label", "tone", "answered_at")
    list_filter = ("tone",)
    search_fields = ("label",)
    raw_id_fields = ("analysis", "website", "prompt")


@admin.register(EvidencePage)
class EvidencePageAdmin(admin.ModelAdmin):
    list_display = ("website", "url", "status_code", "error_code", "read_at")
    raw_id_fields = ("website",)


@admin.register(SiteAudit)
class SiteAuditAdmin(admin.ModelAdmin):
    list_display = ("website", "brand_key", "domain", "audited_at")
    raw_id_fields = ("run", "website")


@admin.register(SiteAuditPage)
class SiteAuditPageAdmin(admin.ModelAdmin):
    list_display = ("audit", "url", "status_code", "passed", "total", "fetched_at")
    raw_id_fields = ("audit",)


@admin.register(PromptIdea)
class PromptIdeaAdmin(admin.ModelAdmin):
    list_display = ("website", "text", "status", "seen_times", "last_seen_at")
    list_filter = ("status",)
    search_fields = ("text",)
    raw_id_fields = ("website",)


@admin.register(SerpFeatureCapture)
class SerpFeatureCaptureAdmin(admin.ModelAdmin):
    list_display = ("website", "prompt", "location", "captured_at")
    raw_id_fields = ("website", "run", "prompt")


@admin.register(PromptSuggestion)
class PromptSuggestionAdmin(admin.ModelAdmin):
    list_display = ("website", "text", "basis", "created_at")
    raw_id_fields = ("website", "run")


@admin.register(FanoutGoogleCheck)
class FanoutGoogleCheckAdmin(admin.ModelAdmin):
    list_display = ("website", "query", "position", "error_code", "checked_at")
    search_fields = ("query",)
    raw_id_fields = ("website", "run", "prompt")


@admin.register(TrackedProduct)
class TrackedProductAdmin(admin.ModelAdmin):
    list_display = ("website", "name", "is_archived", "created_at")
    list_filter = ("is_archived",)
    raw_id_fields = ("website", "created_by")


@admin.register(ShoppingResult)
class ShoppingResultAdmin(admin.ModelAdmin):
    list_display = ("product", "query", "engine", "status", "state", "checked_at")
    list_filter = ("status", "engine")
    raw_id_fields = ("run", "website", "product")
