from django.contrib import admin

from apps.prompt_library.models import (
    Industry,
    Prompt,
    PromptSampleEntry,
    PromptSampleRun,
    PromptVariableSet,
    PromptVariation,
)


@admin.register(Industry)
class IndustryAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Prompt)
class PromptAdmin(admin.ModelAdmin):
    list_display = (
        "text_short",
        "industry",
        "intent_bucket",
        "style",
        "source",
        "source_score",
        "effectiveness_score",
        "runs_count",
        "last_used_at",
        "is_active",
    )
    list_filter = ("intent_bucket", "style", "source", "is_active", "industry")
    search_fields = ("text", "template_text")
    raw_id_fields = ("industry",)
    readonly_fields = (
        "effectiveness_score",
        "effectiveness_components",
        "runs_count",
        "last_used_at",
    )

    @admin.display(description="Text")
    def text_short(self, obj):
        body = obj.template_text or obj.text or ""
        return body[:80]

    @admin.display(description="Source score", ordering="demand_score")
    def source_score(self, obj):
        return obj.demand_score


@admin.register(PromptVariation)
class PromptVariationAdmin(admin.ModelAdmin):
    list_display = ("parent_prompt", "created_at")
    raw_id_fields = ("parent_prompt",)


class PromptSampleEntryInline(admin.TabularInline):
    model = PromptSampleEntry
    extra = 0
    raw_id_fields = ("prompt",)


@admin.register(PromptSampleRun)
class PromptSampleRunAdmin(admin.ModelAdmin):
    list_display = ("audit_run", "industry", "strategy", "seed", "created_at")
    raw_id_fields = ("audit_run", "industry")
    inlines = [PromptSampleEntryInline]


@admin.register(PromptVariableSet)
class PromptVariableSetAdmin(admin.ModelAdmin):
    list_display = ("website", "updated_at")
    raw_id_fields = ("website",)
    search_fields = ("website__name",)
