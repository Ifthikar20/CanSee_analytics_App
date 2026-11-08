from django.urls import path

from apps.billing.polar_webhooks import PolarWebhookView

from . import views
from .health import BillingHealthView

urlpatterns = [
    path("", views.BillingOverviewView.as_view(), name="billing-overview"),
    path("plans/", views.PlansView.as_view(), name="billing-plans"),
    path("checkout/", views.CheckoutView.as_view(), name="billing-checkout"),
    path("confirm/", views.CheckoutConfirmView.as_view(), name="billing-confirm"),
    path("paywall/dismiss/", views.PaywallDismissView.as_view(), name="billing-paywall-dismiss"),
    path("cancel/", views.SubscriptionCancelView.as_view(), name="billing-cancel"),
    path("resume/", views.SubscriptionResumeView.as_view(), name="billing-resume"),
    path("upgrade-now/", views.UpgradeNowView.as_view(), name="billing-upgrade-now"),
    path("change-plan/", views.ChangePlanView.as_view(), name="billing-change-plan"),
    path("portal/", views.PortalView.as_view(), name="billing-portal"),
    path("token-usage/", views.AITokenUsageView.as_view(), name="billing-token-usage"),
    path("prompt-runs/", views.PromptRunsView.as_view(), name="billing-prompt-runs"),
    path("health/", BillingHealthView.as_view(), name="billing-health"),
    path("polar/webhook/", PolarWebhookView.as_view(), name="billing-polar-webhook"),
]
