from rest_framework import status
from rest_framework.response import Response

from apps.notifications.api.v1.serializers import (
    IntegrationConnectionSerializer,
    NotificationPreferenceSerializer,
    NotificationSerializer,
)
from apps.notifications.models import IntegrationConnection, Notification, NotificationPreference
from apps.notifications.services.notification_service import NotificationService
from core.views import TenantScopedAPIView, TenantScopedListAPIView


class NotificationListView(TenantScopedListAPIView):
    """User-scoped (not website-scoped). Uses the base for auth + pagination."""

    def get(self, request):
        notifications = Notification.objects.filter(user=request.user)
        return self.paginated_response(notifications, NotificationSerializer)


class UnreadNotificationsView(TenantScopedAPIView):
    def get(self, request):
        notifications = Notification.objects.filter(user=request.user, read=False)
        count = notifications.count()
        serializer = NotificationSerializer(notifications[:10], many=True)
        return Response({"count": count, "notifications": serializer.data})


class NotificationReadView(TenantScopedAPIView):
    def put(self, request, pk):
        notification = NotificationService.mark_read(notification_id=pk, user=request.user)
        return Response(NotificationSerializer(notification).data)


class ReadAllNotificationsView(TenantScopedAPIView):
    def post(self, request):
        count = NotificationService.mark_all_read(user=request.user)
        return Response({"marked_read": count})


class NotificationDetailView(TenantScopedAPIView):
    def delete(self, request, pk):
        Notification.objects.filter(id=pk, user=request.user).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class NotificationPreferencesView(TenantScopedAPIView):
    def get(self, request):
        prefs, _ = NotificationPreference.objects.get_or_create(user=request.user)
        return Response(NotificationPreferenceSerializer(prefs).data)

    def put(self, request):
        prefs, _ = NotificationPreference.objects.get_or_create(user=request.user)
        serializer = NotificationPreferenceSerializer(prefs, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


# ── Integration Connections (Slack / Discord) ─────────────────────────────────


# Platform host allowlist for incoming-webhook URLs. Anything else is
# rejected so a connection can never post Cansee data to an arbitrary host.
WEBHOOK_URL_PREFIXES = {
    "slack": ("https://hooks.slack.com/",),
    "discord": (
        "https://discord.com/api/webhooks/",
        "https://discordapp.com/api/webhooks/",
    ),
}

# Teams incoming-webhook hosts use a per-tenant subdomain, so a fixed prefix
# can't match. Validate by host suffix instead: the classic connector
# (*.webhook.office.com) and the newer Workflows trigger (*.logic.azure.com).
WEBHOOK_URL_HOST_SUFFIXES = {
    "teams": (".webhook.office.com", ".logic.azure.com"),
}


def _validate_webhook_url(platform: str, url: str) -> str:
    """Return an error message when the URL is not allowed for the platform."""
    prefixes = WEBHOOK_URL_PREFIXES.get(platform)
    if prefixes:
        if not url.startswith(prefixes):
            allowed = " or ".join(f"{p}..." for p in prefixes)
            return f"Invalid {platform} webhook URL. Expected {allowed}"
        return ""

    suffixes = WEBHOOK_URL_HOST_SUFFIXES.get(platform)
    if suffixes:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https":
            return f"Invalid {platform} webhook URL. It must start with https://"
        if not any(host.endswith(suffix) for suffix in suffixes):
            return (
                "Invalid Microsoft Teams webhook URL. Expected a "
                "*.webhook.office.com or *.logic.azure.com address."
            )
        return ""

    return f"Unsupported platform: {platform}"


class IntegrationConnectionListView(TenantScopedAPIView):
    """
    GET  — list all connected integrations for the current user.
    POST — create or update a connection (upsert by platform).
    """

    def get(self, request):
        connections = IntegrationConnection.objects.filter(user=request.user)
        serializer = IntegrationConnectionSerializer(connections, many=True)
        return Response({"data": serializer.data})

    def post(self, request):
        platform = request.data.get("platform", "")
        if platform not in dict(IntegrationConnection.PLATFORM_CHOICES):
            return Response(
                {"error": f"Invalid platform: {platform}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        payload = request.data.copy()
        if "webhook_url" in payload:
            webhook_url = (payload.get("webhook_url") or "").strip()
            if webhook_url:
                error = _validate_webhook_url(platform, webhook_url)
                if error:
                    return Response({"error": error}, status=status.HTTP_400_BAD_REQUEST)
                payload["webhook_url"] = webhook_url
            else:
                # Blank on update = keep the stored webhook. The field is
                # write-only (never read back), so the UI can't resend it;
                # dropping it here preserves the existing value instead of
                # wiping the connection when only other fields change.
                payload.pop("webhook_url", None)

        # Upsert: one connection per user per platform
        connection, created = IntegrationConnection.objects.get_or_create(
            user=request.user,
            platform=platform,
            defaults={"webhook_url": ""},
        )

        serializer = IntegrationConnectionSerializer(
            connection, data=payload, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()

        # Send a test message on first connect
        if created or not connection.is_active:
            connection.is_active = True
            connection.save(update_fields=["is_active"])
            self._send_test_message(connection)

        return Response(
            {"data": IntegrationConnectionSerializer(connection).data},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @staticmethod
    def _send_test_message(connection):
        """Send a test/welcome message to verify the webhook works."""
        import logging
        logger = logging.getLogger("apps")
        try:
            if connection.platform == "slack":
                from apps.notifications.services.slack_service import SlackService
                SlackService.send_message(
                    webhook_url=connection.webhook_url,
                    text="Cansee connected. You'll receive the daily report here.",
                )
            elif connection.platform == "discord":
                from apps.notifications.services.discord_service import DiscordService
                DiscordService.send_message(
                    webhook_url=connection.webhook_url,
                    title="Cansee Connected",
                    # Name only what actually sends today: the daily report.
                    description="You'll receive the daily report here.",
                )
            elif connection.platform == "teams":
                from apps.notifications.services.teams_service import TeamsService
                TeamsService.send_message(
                    webhook_url=connection.webhook_url,
                    title="Cansee connected",
                    text="You'll receive the daily report here.",
                )
        except Exception as e:
            logger.warning(f"Test message failed for {connection.platform}: {e}")


class IntegrationConnectionDetailView(TenantScopedAPIView):
    """
    PUT    — update an integration connection.
    DELETE — disconnect (delete) an integration.
    """

    def put(self, request, pk):
        try:
            connection = IntegrationConnection.objects.get(id=pk, user=request.user)
        except IntegrationConnection.DoesNotExist:
            return Response({"error": "Connection not found"}, status=status.HTTP_404_NOT_FOUND)

        payload = request.data.copy()
        if "webhook_url" in payload:
            webhook_url = (payload.get("webhook_url") or "").strip()
            if webhook_url:
                platform = payload.get("platform") or connection.platform
                error = _validate_webhook_url(platform, webhook_url)
                if error:
                    return Response({"error": error}, status=status.HTTP_400_BAD_REQUEST)
            payload["webhook_url"] = webhook_url

        serializer = IntegrationConnectionSerializer(connection, data=payload, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({"data": serializer.data})

    def delete(self, request, pk):
        IntegrationConnection.objects.filter(id=pk, user=request.user).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

