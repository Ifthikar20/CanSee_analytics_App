from django.contrib import admin

from apps.search_gap.models import CountryCheckResult, CountryCheckRun, GoogleRankCheck


@admin.register(GoogleRankCheck)
class GoogleRankCheckAdmin(admin.ModelAdmin):
    list_display = ("query", "website", "position", "pages_fetched", "cost_usd", "error", "checked_at")
    list_filter = ("error",)
    search_fields = ("query", "url")
    raw_id_fields = ("website", "brand_prompt")
    readonly_fields = ("checked_at",)


@admin.register(CountryCheckRun)
class CountryCheckRunAdmin(admin.ModelAdmin):
    list_display = ("website", "status", "countries", "created_at")
    list_filter = ("status",)
    raw_id_fields = ("website", "created_by")
    readonly_fields = ("created_at", "started_at", "finished_at", "heartbeat_at")


@admin.register(CountryCheckResult)
class CountryCheckResultAdmin(admin.ModelAdmin):
    list_display = ("run", "country", "engine", "status", "position", "mentioned")
    list_filter = ("status", "engine")
    raw_id_fields = ("run", "website", "brand_prompt")
    readonly_fields = ("created_at", "started_at", "finished_at")
