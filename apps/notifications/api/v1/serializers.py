from rest_framework import serializers

from apps.notifications.models import IntegrationConnection, Notification, NotificationPreference

# Incoming-webhook host allowlist. A webhook URL is a bearer credential
# AND an outbound-request target, so it must point only at the provider's
# own host — never an internal/metadata address (SSRF).
_SLACK_WEBHOOK_PREFIX = "https://hooks.slack.com/"


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = ["id", "type", "title", "message", "data", "read", "action_url", "created_at"]
        read_only_fields = fields


class NotificationPreferenceSerializer(serializers.ModelSerializer):
    # write_only: the Slack webhook is a secret; never echo it back in a
    # read (browser memory / logs / a settings-page XSS would harvest it).
    # The UI shows configured-state via the boolean instead.
    slack_webhook_url = serializers.CharField(
        write_only=True, required=False, allow_blank=True,
    )
    slack_webhook_configured = serializers.SerializerMethodField()

    class Meta:
        model = NotificationPreference
        fields = [
            "hot_lead_email", "hot_lead_slack", "weekly_report",
            "competitor_changes", "audit_complete", "slack_webhook_url",
            "slack_webhook_configured",
        ]

    def get_slack_webhook_configured(self, obj) -> bool:
        return bool(obj.slack_webhook_url)

    def validate_slack_webhook_url(self, value):
        v = (value or "").strip()
        if v and not v.startswith(_SLACK_WEBHOOK_PREFIX):
            # Blocks blind SSRF: without this a user could point the
            # webhook at http://169.254.169.254/... and have Cansee POST
            # to internal/metadata hosts.
            raise serializers.ValidationError(
                "Enter a valid Slack incoming-webhook URL "
                "(https://hooks.slack.com/...)."
            )
        return v


class IntegrationConnectionSerializer(serializers.ModelSerializer):
    # write_only for the same reason as above; the view host-validates it
    # on write, and blank-on-update preserves the stored value.
    webhook_url = serializers.CharField(
        write_only=True, required=False, allow_blank=True,
    )
    webhook_configured = serializers.SerializerMethodField()
    # True once a signed /cansee link command proved the workspace.
    external_team_linked = serializers.SerializerMethodField()

    class Meta:
        model = IntegrationConnection
        fields = [
            "id", "platform", "webhook_url", "webhook_configured",
            "channel_name", "is_active",
            "external_team_id", "external_team_linked", "external_channel_id",
            "schedule_time", "frequency", "message_format",
            "notify_daily_report", "notify_hot_leads",
            "notify_trend_digest", "notify_milestones",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_webhook_configured(self, obj) -> bool:
        return bool(obj.webhook_url)

    def get_external_team_linked(self, obj) -> bool:
        return bool(obj.external_team_id and obj.external_team_verified_at)

    def validate_external_team_id(self, value):
        """The workspace id is only set by a verified link (chat_link). Here
        it can be cleared (unlink) or sent back unchanged, nothing else."""
        value = (value or "").strip()
        current = getattr(self.instance, "external_team_id", "") or ""
        if value and value != current:
            raise serializers.ValidationError(
                "Link a workspace by running /cansee link with a link code "
                "in that workspace. Workspace ids can't be entered here."
            )
        return value

    def update(self, instance, validated_data):
        if "external_team_id" in validated_data and not validated_data["external_team_id"]:
            validated_data["external_team_verified_at"] = None
        return super().update(instance, validated_data)
