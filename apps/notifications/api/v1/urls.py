from django.urls import path

from apps.notifications.api.v1 import sms_views, sms_webhooks

from . import chat_webhooks, views

urlpatterns = [
    path("", views.NotificationListView.as_view(), name="notification-list"),
    path("unread/", views.UnreadNotificationsView.as_view(), name="notification-unread"),
    path("read-all/", views.ReadAllNotificationsView.as_view(), name="notification-read-all"),
    path("preferences/", views.NotificationPreferencesView.as_view(), name="notification-preferences"),
    path("<uuid:pk>/read/", views.NotificationReadView.as_view(), name="notification-read"),
    path("<uuid:pk>/", views.NotificationDetailView.as_view(), name="notification-detail"),

    # Integration connections (Slack / Discord)
    path("integrations/", views.IntegrationConnectionListView.as_view(), name="integration-list"),
    path("integrations/<uuid:pk>/", views.IntegrationConnectionDetailView.as_view(), name="integration-detail"),
    path(
        "integrations/<uuid:pk>/link-code/",
        views.IntegrationLinkCodeView.as_view(),
        name="integration-link-code",
    ),

    # Inbound chat-platform webhooks (signature-verified, unauthenticated)
    path("discord/interactions/", chat_webhooks.discord_interactions, name="discord-interactions"),
    path("slack/events/", chat_webhooks.slack_events, name="slack-events"),
    path("slack/commands/", chat_webhooks.slack_commands, name="slack-commands"),
    path(
        "sms/inbound/",
        sms_webhooks.twilio_inbound,
        name="notifications-sms-inbound",
    ),

    # SMS subscriptions (self-serve verify / manage / opt out)
    path("sms/", sms_views.SmsSubscriptionListView.as_view(), name="sms-subscription-list"),
    path("sms/<uuid:pk>/verify/", sms_views.SmsVerifyView.as_view(), name="sms-subscription-verify"),
    path("sms/<uuid:pk>/resend/", sms_views.SmsResendView.as_view(), name="sms-subscription-resend"),
    path("sms/<uuid:pk>/", sms_views.SmsSubscriptionDetailView.as_view(), name="sms-subscription-detail"),
]
