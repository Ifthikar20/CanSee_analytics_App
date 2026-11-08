from django.urls import path

from . import views

urlpatterns = [
    path("register/", views.RegisterView.as_view(), name="auth-register"),
    path("login/", views.LoginView.as_view(), name="auth-login"),
    path("logout/", views.LogoutView.as_view(), name="auth-logout"),
    path("refresh/", views.TokenRefreshView.as_view(), name="auth-refresh"),
    path("verify-email/", views.VerifyEmailView.as_view(), name="auth-verify-email"),
    path("resend-verification/", views.ResendVerificationView.as_view(), name="auth-resend-verification"),
    path("forgot-password/", views.ForgotPasswordView.as_view(), name="auth-forgot-password"),
    path("reset-password/", views.ResetPasswordView.as_view(), name="auth-reset-password"),
    path("change-password/", views.ChangePasswordView.as_view(), name="auth-change-password"),
    path("google/", views.GoogleOAuthView.as_view(), name="auth-google"),
    path("microsoft/", views.MicrosoftOAuthView.as_view(), name="microsoft-oauth"),
    path("sso/start/", views.SsoStartView.as_view(), name="auth-sso-start"),
    path("saml/start/", views.SamlStartView.as_view(), name="auth-saml-start"),
    path("saml/callback/", views.SamlCallbackView.as_view(), name="auth-saml-callback"),
    path("token-exchange/", views.TokenExchangeView.as_view(), name="auth-token-exchange"),
    path("me/", views.MeView.as_view(), name="auth-me"),
    path("me/export/", views.MeExportView.as_view(), name="auth-me-export"),
    path("me/ai-usage/", views.AIUsageView.as_view(), name="auth-ai-usage"),
    path("session/", views.SessionView.as_view(), name="auth-session"),
    path("terms/accept/", views.TermsAcceptView.as_view(), name="auth-terms-accept"),
]
