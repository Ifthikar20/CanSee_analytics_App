from django.contrib import admin

from apps.search_gap.models import GoogleRankCheck


@admin.register(GoogleRankCheck)
class GoogleRankCheckAdmin(admin.ModelAdmin):
    list_display = ("query", "website", "position", "pages_fetched", "cost_usd", "error", "checked_at")
    list_filter = ("error",)
    search_fields = ("query", "url")
    raw_id_fields = ("website", "brand_prompt")
    readonly_fields = ("checked_at",)
