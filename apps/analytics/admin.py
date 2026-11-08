from django.contrib import admin

from apps.analytics.models import (
    AnalyticsAccessLog,
    LinkClick,
    PageEvent,
    TrackedLink,
    Visitor,
)


@admin.register(AnalyticsAccessLog)
class AnalyticsAccessLogAdmin(admin.ModelAdmin):
    list_display = ("accessed_at", "user", "website_id_raw", "path", "status_code")
    list_filter = ("status_code", "method")
    search_fields = ("user__email", "website_id_raw", "path")
    list_select_related = ("user",)
    readonly_fields = tuple(f.name for f in AnalyticsAccessLog._meta.fields)
    date_hierarchy = "accessed_at"

    def has_add_permission(self, request):
        return False  # append-only; never hand-edit the audit trail

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Visitor)
class VisitorAdmin(admin.ModelAdmin):
    list_display = ("fingerprint_hash", "website", "geo_country", "visit_count", "last_seen")
    list_filter = ("geo_country", "device_type")
    search_fields = ("fingerprint_hash",)
    readonly_fields = ("id", "fingerprint_hash", "first_seen", "last_seen")


@admin.register(PageEvent)
class PageEventAdmin(admin.ModelAdmin):
    list_display = ("event_type", "url", "visitor", "timestamp")
    list_filter = ("event_type",)
    readonly_fields = ("id", "timestamp")


@admin.register(TrackedLink)
class TrackedLinkAdmin(admin.ModelAdmin):
    list_display = ("id", "website", "tracking_key", "destination_url", "click_count", "conversion_count", "created_at")
    search_fields = ("tracking_key", "destination_url", "website__name")
    readonly_fields = ("id", "tracking_key", "created_at", "updated_at")
    ordering = ("-created_at",)


@admin.register(LinkClick)
class LinkClickAdmin(admin.ModelAdmin):
    list_display = ("id", "tracked_link", "ip_hash", "converted", "clicked_at")
    list_filter = ("converted",)
    readonly_fields = ("id", "clicked_at")
