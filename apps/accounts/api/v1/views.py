from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.api.v1.serializers import (
    ChangePasswordSerializer,
    ForgotPasswordSerializer,
    LoginSerializer,
    RegisterSerializer,
    ResetPasswordSerializer,
    UserProfileSerializer,
    VerifyEmailSerializer,
)
from apps.accounts.services.auth_service import AuthService
from apps.accounts.services.oauth_service import OAuthService
from apps.accounts.services.user_service import UserService
from core.interceptors.throttling import AuthRateThrottle, PasswordResetThrottle
from core.permissions.org import OrgFeaturesGate


# Cookie attributes for the JWT refresh token.
#
# SameSite=Lax is the CSRF control for this cookie, and it is the only
# one. The refresh/logout endpoints authenticate from the cookie alone,
# so with SameSite=None any origin could make a browser POST to them
# with the victim's credentials attached: forced logout, refresh-token
# churn against ROTATE_REFRESH_TOKENS, and login-CSRF that silently
# drops a victim into an attacker-controlled account. Lax stops the
# browser attaching the cookie to cross-site POSTs at all, which
# removes the precondition rather than mitigating the consequence.
#
# Nothing in production needs cross-site delivery: nginx.prod.conf
# serves the SPA (location /) and the API (location /api/) from the
# same cansee.ai server block, so every request that matters is
# same-origin. The earlier SameSite=None dated from a topology that no
# longer exists.
#
# Note SameSite keys on the registrable domain, not the full host, so
# splitting to app.cansee.ai + api.cansee.ai stays same-site and
# keeps working. Lax also still sends the cookie on top-level GET
# navigations, so the Google Search Console OAuth callback is fine.
#
# Only a genuinely different registrable domain (a *.vercel.app preview,
# say) would force SameSite=None. If that ever happens, do NOT just flip
# this value: SameSite=None reopens the CSRF hole above, so it has to
# come with csrf_protect on the three cookie-reading endpoints, an
# ensure_csrf_cookie bootstrap route, X-CSRFToken on the SPA client, and
# the new origin added to CSRF_TRUSTED_ORIGINS.
#
# Dev (DEBUG=True) additionally relaxes Secure: browsers grant a
# localhost exemption, but Vite's proxy over http makes delivery flaky,
# which kicks developers to /login.
def _refresh_cookie_settings():
    """Cookie kwargs for the refresh token, resolved per call.

    ``secure`` follows ``SESSION_COOKIE_SECURE``, which base.py derives from
    PUBLIC_SCHEME -- so this cookie can never disagree with the session and
    CSRF cookies about whether the deployment is on TLS. That matters
    because a Secure cookie is silently discarded by the browser over
    http://: on a plaintext deployment, login would return 200 and the
    session would then die on the first reload, with nothing in the logs.

    DEBUG still forces it off regardless: Vite proxies over http locally, so
    Secure would drop the cookie and bounce developers to /login.

    This is deliberately NOT cached in a module-level constant. It used to
    be, which froze the value at import time under whatever DEBUG was then
    and made it unreachable by settings -- see TestRefreshCookieBinding.
    """
    from django.conf import settings as _settings

    secure = bool(getattr(_settings, "SESSION_COOKIE_SECURE", True))
    if getattr(_settings, "DEBUG", False):
        secure = False

    # The cookie must outlive the token, never the reverse: this used to
    # hardcode 7 days against a 60-day REFRESH_TOKEN_LIFETIME, silently
    # making the cookie the real session ceiling.
    lifetime = _settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"]

    return {
        "key": "refresh_token",
        "httponly": True,
        "secure": secure,
        "samesite": "Lax",
        "max_age": int(lifetime.total_seconds()),
        "path": "/",
    }


class RegisterView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [AuthRateThrottle]

    def post(self, request):
        # Beta gate: when SIGNUPS_ENABLED is False, refuse new accounts at the
        # API layer too so the closed signup can't be bypassed by anyone hitting
        # the endpoint directly.
        from django.conf import settings as dj_settings

        if not getattr(dj_settings, "SIGNUPS_ENABLED", True):
            return Response(
                {
                    "error": {
                        "code": "signups_closed",
                        "message": (
                            "Cansee is in private beta. New sign-ups are paused — "
                            "please contact us for access."
                        ),
                    }
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = AuthService.register(**serializer.validated_data)

        from apps.accounts.tasks import send_verification_email
        send_verification_email.delay(str(user.id))

        return Response(
            {"message": "Account created. Please check your email to verify."},
            status=status.HTTP_201_CREATED,
        )


class LoginView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [AuthRateThrottle]

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        result = AuthService.login(
            **serializer.validated_data,
            ip_address=request.META.get("REMOTE_ADDR", ""),
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
            request=request,
        )

        response = Response(
            {"access": result["access"], "user": result["user"]},
            status=status.HTTP_200_OK,
        )
        response.set_cookie(value=result["refresh"], **_refresh_cookie_settings())
        return response


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        refresh_token = request.COOKIES.get("refresh_token")
        AuthService.logout(refresh_token=refresh_token, user=request.user)

        response = Response({"message": "Successfully logged out."}, status=status.HTTP_200_OK)
        response.delete_cookie("refresh_token", path="/")
        return response


class TokenRefreshView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = []  # No throttling — called on every page load for session restore

    def post(self, request):
        refresh_token = request.COOKIES.get("refresh_token")
        if not refresh_token:
            return Response(
                {"error": "No refresh token provided."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        result = AuthService.refresh_token(refresh_token=refresh_token)

        response = Response({"access": result["access"]}, status=status.HTTP_200_OK)
        response.set_cookie(value=result["refresh"], **_refresh_cookie_settings())
        return response


class VerifyEmailView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = VerifyEmailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        AuthService.verify_email_otp(**serializer.validated_data)
        return Response({"message": "Email verified successfully."})


class ResendVerificationView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [AuthRateThrottle]

    def post(self, request):
        email = request.data.get("email", "")
        from apps.accounts.models import User
        try:
            user = User.objects.get(email__iexact=email, is_email_verified=False)
            from apps.accounts.tasks import send_verification_email
            send_verification_email.delay(str(user.id))
        except User.DoesNotExist:
            pass  # Don't reveal whether email exists
        return Response({"message": "If an unverified account exists, a new code has been sent."})


class ForgotPasswordView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [PasswordResetThrottle]

    def post(self, request):
        serializer = ForgotPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        token = AuthService.generate_password_reset_token(email=serializer.validated_data["email"])
        if token:
            from apps.accounts.tasks import send_password_reset_email
            send_password_reset_email.delay(serializer.validated_data["email"], token)
        return Response({"message": "If an account exists with that email, a reset link has been sent."})


class ResetPasswordView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = ResetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        AuthService.reset_password(**serializer.validated_data)
        return Response({"message": "Password reset successfully."})


class ChangePasswordView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        AuthService.change_password(user=request.user, **serializer.validated_data)
        return Response({"message": "Password changed successfully."})


class GoogleOAuthView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [AuthRateThrottle]

    def post(self, request):
        code = request.data.get("code")
        redirect_uri = request.data.get("redirect_uri", "")
        invite_token = request.data.get("invite_token", "") or ""
        if not code:
            return Response({"error": "Authorization code required."}, status=status.HTTP_400_BAD_REQUEST)

        auth = OAuthService.google_authenticate(
            code=code, redirect_uri=redirect_uri, invite_token=invite_token
        )
        user = auth["user"]

        # Google verified the mailbox (email_verified checked against the
        # signed id_token), which satisfies our own verification gate — a
        # provisioned-but-never-verified account must not be locked out of
        # the Google button it has always used.
        if not user.is_email_verified:
            user.is_email_verified = True
            user.save(update_fields=["is_email_verified"])

        from apps.accounts.services.token_service import TokenService

        result = TokenService.issue_session(
            user,
            method="google",
            ip_address=request.META.get("REMOTE_ADDR", ""),
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
        )

        payload = {
            "access": result["access"],
            "user": result["user"],
            "joined_org": auth["joined_org"],
            "is_new_user": auth["is_new_user"],
        }
        if auth["org"] is not None:
            payload["org"] = {
                "id": str(auth["org"].id),
                "name": auth["org"].name,
                "slug": auth["org"].slug,
                "logo_url": auth["org"].logo_url,
            }
        response = Response(payload)
        response.set_cookie(value=result["refresh"], **_refresh_cookie_settings())
        return response


class MicrosoftOAuthView(OrgFeaturesGate, APIView):
    permission_classes = [AllowAny]
    throttle_classes = [AuthRateThrottle]

    def post(self, request):
        code = request.data.get("code")
        redirect_uri = request.data.get("redirect_uri", "")
        invite_token = request.data.get("invite_token", "") or ""
        if not code:
            return Response({"error": "Authorization code required."}, status=status.HTTP_400_BAD_REQUEST)

        auth = OAuthService.entra_authenticate(
            code=code, redirect_uri=redirect_uri, invite_token=invite_token
        )
        user = auth["user"]

        # The service only accepted this sign-in through a lane that
        # vouches for the mailbox (stable identity, invitation token, or a
        # tenant-registered verified domain) — same reasoning as Google: a
        # provisioned-but-never-verified account must not be locked out of
        # the SSO button it has always used.
        if not user.is_email_verified:
            user.is_email_verified = True
            user.save(update_fields=["is_email_verified"])

        from apps.accounts.services.token_service import TokenService

        result = TokenService.issue_session(
            user,
            method="entra",
            ip_address=request.META.get("REMOTE_ADDR", ""),
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
        )

        payload = {
            "access": result["access"],
            "user": result["user"],
            "joined_org": auth["joined_org"],
            "is_new_user": auth["is_new_user"],
        }
        if auth["org"] is not None:
            payload["org"] = {
                "id": str(auth["org"].id),
                "name": auth["org"].name,
                "slug": auth["org"].slug,
                "logo_url": auth["org"].logo_url,
            }
        response = Response(payload)
        response.set_cookie(value=result["refresh"], **_refresh_cookie_settings())
        return response


class SsoStartView(OrgFeaturesGate, APIView):
    """Enterprise SSO discovery: work email in, identity route out.

    Powers the /sso page: the user types their company email, and this
    resolves the DOMAIN (never the account) to a claimed-and-verified
    OrgDomain, answering which IdP buttons to show. Deliberately
    domain-keyed so it can't be used to probe whether a specific person
    has an account — the only fact it reveals is "this company runs SSO
    on Cansee", which the login page's own SSO panel reveals anyway.
    """

    permission_classes = [AllowAny]
    throttle_classes = [AuthRateThrottle]

    def post(self, request):
        from apps.accounts.models import OrgDomain
        from core.exceptions import ResourceNotFound

        email = (request.data.get("email") or "").strip().lower()
        if "@" not in email:
            return Response(
                {"error": {"code": "validation_error",
                           "message": "Enter your work email address."}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        domain = email.rsplit("@", 1)[-1]

        record = (
            OrgDomain.objects.select_related("organization")
            .filter(domain=domain, verified_at__isnull=False)
            .first()
        )
        if record is None:
            raise ResourceNotFound(
                "Single sign-on isn't set up for this email domain."
            )

        org = record.organization
        return Response({
            "org_name": org.name,
            "domain": domain,
            "methods": AuthService.sso_methods_for(org),
            "login_hint": email,
        })


class SamlStartView(OrgFeaturesGate, APIView):
    """Begin the WorkOS SAML flow: work email in, authorize URL out.

    Domain-keyed like SsoStartView — the answer is identical whether or
    not an account exists for the address.
    """

    permission_classes = [AllowAny]
    throttle_classes = [AuthRateThrottle]

    def post(self, request):
        from apps.accounts.services import saml_service

        email = (request.data.get("email") or "").strip().lower()
        if "@" not in email:
            return Response(
                {"error": {"code": "validation_error",
                           "message": "Enter your work email address."}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({"redirect": saml_service.authorize_redirect(email=email)})


class SamlCallbackView(APIView):
    """Browser redirect target for the WorkOS hosted flow — NOT an XHR.

    Success hands the browser back to the SPA with a one-time exchange
    code (?c=...) that the SPA immediately trades at /auth/token-exchange/.
    Tokens NEVER ride the URL: URLs leak through history, proxies, and
    Referer headers. Failures redirect with ?err=<code> only — no message
    text in the URL either.
    """

    permission_classes = [AllowAny]

    def get(self, request):
        import requests
        from django.conf import settings as dj_settings
        from django.http import HttpResponseRedirect

        from apps.accounts.services import saml_service
        from core.exceptions import CanseeException

        spa = dj_settings.FRONTEND_URL.rstrip("/")

        def _fail(code: str = "sso_failed"):
            return HttpResponseRedirect(f"{spa}/auth/sso/complete?err={code}")

        # Master business-features switch: a browser is waiting, so the
        # gate answers a readable redirect, not the JSON 404 the XHR
        # endpoints use.
        from core.permissions.org import org_features_enabled

        if not org_features_enabled():
            return _fail()

        # WorkOS answers with ?code&state; SSOReady with ?saml_access_code
        # (our signed state rides through their API and returns on redeem).
        code = (
            request.query_params.get("saml_access_code")
            or request.query_params.get("code")
            or ""
        )
        state = request.query_params.get("state") or ""
        if not code:
            return _fail()

        try:
            auth = saml_service.complete(code=code, state=state)
        except CanseeException as exc:
            return _fail(exc.code)
        except (ValueError, KeyError):
            return _fail()
        except requests.RequestException:
            # The WorkOS token endpoint failed or answered garbage — a
            # browser is waiting, so it must land somewhere readable.
            return _fail()

        user = auth["user"]
        # The IdP asserted this mailbox through an org-restricted
        # connection — same reasoning as the Google/Entra lanes: a
        # provisioned-but-never-verified account must not be locked out
        # of the SSO button it has always used.
        if not user.is_email_verified:
            user.is_email_verified = True
            user.save(update_fields=["is_email_verified"])

        from apps.accounts.services.token_service import TokenService

        try:
            result = TokenService.issue_session(
                user,
                method="saml",
                ip_address=request.META.get("REMOTE_ADDR", ""),
                user_agent=request.META.get("HTTP_USER_AGENT", ""),
            )
        except ValueError:
            return _fail()

        payload = {
            "access": result["access"],
            "refresh": result["refresh"],
            "user": result["user"],
            "joined_org": auth["joined_org"],
            "is_new_user": auth["is_new_user"],
        }
        if auth["org"] is not None:
            payload["org"] = {
                "id": str(auth["org"].id),
                "name": auth["org"].name,
                "slug": auth["org"].slug,
                "logo_url": auth["org"].logo_url,
            }

        xc = saml_service.mint_exchange_code(payload)
        return HttpResponseRedirect(f"{spa}/auth/sso/complete?c={xc}")


class TokenExchangeView(OrgFeaturesGate, APIView):
    """Trade a one-time browser-handoff code for the real session."""

    permission_classes = [AllowAny]
    throttle_classes = [AuthRateThrottle]

    def post(self, request):
        from apps.accounts.services import saml_service
        from core.exceptions import CanseeException

        code = request.data.get("code") or ""
        payload = saml_service.redeem_exchange_code(code) if code else None
        if payload is None:
            raise CanseeException(
                "This sign-in expired — try again.",
                code="invalid_exchange_code",
                status_code=400,
            )

        refresh = payload.pop("refresh", "")
        response = Response(payload)
        if refresh:
            response.set_cookie(value=refresh, **_refresh_cookie_settings())
        return response


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        serializer = UserProfileSerializer(request.user)
        return Response(serializer.data)

    def put(self, request):
        serializer = UserProfileSerializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    patch = put

    def delete(self, request):
        UserService.delete_account(user=request.user)
        return Response({"message": "Account deleted."}, status=status.HTTP_200_OK)


class MeExportView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        data = UserService.export_data(user=request.user)
        return Response(data)


class SessionView(APIView):
    """
    Post-login bootstrap. Returns the minimum the frontend needs to decide
    where to route the user: onboarding, paywall, or app.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from django.conf import settings

        from apps.accounts.services.org_service import OrgService
        from apps.billing.services import polar_billing
        from apps.billing.services.plan_limits import limits_block, subscription_state
        from apps.websites.services.website_service import WebsiteService

        user = request.user

        # Accessible = owned OR reachable through org membership. Counting
        # only OWNED websites here used to dump every org invitee into the
        # create-a-project wizard on their first login.
        websites = list(
            WebsiteService.accessible_qs(user).filter(is_active=True).only(
                "id", "name", "url"
            )
        )
        needs_onboarding = not websites

        # Master switch: while business flows are off, every account is
        # plain B2C — no org lookup (saves a query per session call too).
        from core.permissions.org import org_features_enabled

        membership = (
            OrgService.membership_for(user) if org_features_enabled() else None
        )
        org_block = None
        if membership:
            org = membership.organization
            org_block = {
                "id": str(org.id),
                "name": org.name,
                "slug": org.slug,
                "logo_url": org.logo_url,
                "role": membership.role,
                "sso_enforced": org.require_sso,
            }

        sub = getattr(user, "subscription", None)
        # A trial whose end date passed without a conversion webhook
        # (dev has none; prod can lag) is settled against Polar here,
        # cooldown-limited, so the row below is the real state.
        sub = polar_billing.reverify_ended_trial(user, sub)
        # One builder for the whole subscription block so every surface
        # labels a trial, a paid plan and a lapsed row the same way.
        subscription = subscription_state(sub)
        if membership:
            # The org's provisioned plan is the member's entitlement; their
            # personal subscription state (usually none) must not label the
            # UI or gate the paywall.
            from apps.billing.services.plan_limits import _LEGACY_MAP

            org_plan = _LEGACY_MAP.get(
                membership.organization.plan, membership.organization.plan
            )
            subscription = {
                **subscription,
                "plan": str(org_plan),
                "tier": str(org_plan),
                "is_paying": True,
                "source": "organization",
            }
        else:
            subscription["source"] = "user"
        is_paying = subscription["is_paying"]

        # Onboarding first, then paywall. We want users to see the
        # value (their topics, competitors, tracked brands) before
        # they're asked to pay — invested users convert better. So
        # the funnel is: register -> onboarding modal -> paywall ->
        # dashboard.
        # The paywall step is skipped entirely while PAYWALL_ENABLED is
        # False (the flag lives in settings and is env-driven, so it can
        # be flipped back on without a code change), and permanently for
        # users who chose the Free plan from the paywall once
        # (paywall_dismissed_at set via POST /billing/paywall/dismiss/).
        if needs_onboarding:
            next_route = "onboarding"
        elif (
            not is_paying
            and settings.PAYWALL_ENABLED
            and user.paywall_dismissed_at is None
        ):
            next_route = "paywall"
        else:
            next_route = "app"

        # Projects, tracked prompts ({limit, used}), Autopilot and the daily
        # text, as the billing overview shows them; org members add seats.
        limits = limits_block(user)
        if membership:
            from apps.billing.services.org_entitlements import seats_block

            seats = seats_block(membership.organization)
            if seats:
                limits["seats"] = seats

        return Response({
            "user": UserProfileSerializer(user).data,
            "onboarding": {
                "needs_onboarding": needs_onboarding,
                "websites_count": len(websites),
            },
            "org": org_block,
            "subscription": subscription,
            # Resolved server-side (trial-aware, paywall-aware) so the UI
            # never re-derives plan limits from the plan name. -1 = unlimited.
            "limits": limits,
            "paywall_dismissed": user.paywall_dismissed_at is not None,
            "next_route": next_route,
        })


class AIUsageView(APIView):
    """
    Centralised AI usage rollup for the authenticated user.

    One source of truth for the Settings "Overall Usage" panel — every AI
    call site writes through core.ai_tracking and rolls up here, broken
    down by module, model, provider, and role.

    The window is always the CURRENT BILLING PERIOD (subscription cycle
    when one exists, calendar month otherwise) — the same window the
    spend wall and cap notifications use, so the page can never disagree
    with enforcement. Totals come from Polar meters once
    POLAR_READS_ENABLED is on, with automatic fallback to the local
    ledger. A legacy ``days`` query param is accepted and ignored.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.metering.services.usage_reader import get_period_usage

        return Response(get_period_usage(request.user))

