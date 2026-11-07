"""Attributes of the JWT refresh-token cookie.

The refresh and logout endpoints authenticate from ``refresh_token``
alone (``apps/accounts/api/v1/views.py`` reads ``request.COOKIES``), so
SameSite is the only thing standing between them and cross-site request
forgery. The API itself is not exposed the same way: DRF authenticates
from an ``Authorization: Bearer`` header that browsers never attach
automatically, and ``APIView.as_view()`` is wrapped in ``csrf_exempt``.
This cookie is the exception, which is why it gets its own test.

These are pure-function assertions on ``_refresh_cookie_settings`` — no
database, no cache, no HTTP client — so they run anywhere.
"""
import pytest
from django.test import override_settings

from apps.accounts.api.v1.views import _refresh_cookie_settings


class TestRefreshCookieSameSite:
    """SameSite=None would let any origin drive the cookie endpoints."""

    @override_settings(DEBUG=False)
    def test_production_is_samesite_lax(self):
        assert _refresh_cookie_settings()["samesite"] == "Lax"

    @override_settings(DEBUG=False)
    def test_production_is_never_samesite_none(self):
        # The regression this file exists for. SameSite=None instructs the
        # browser to attach the cookie to cross-site POSTs, which is the
        # precondition for forced logout, refresh churn, and login-CSRF.
        # Flipping it back demands csrf_protect on the cookie-reading
        # views first - see the comment above _refresh_cookie_settings.
        assert _refresh_cookie_settings()["samesite"] != "None"

    @override_settings(DEBUG=True)
    def test_dev_is_also_samesite_lax(self):
        assert _refresh_cookie_settings()["samesite"] == "Lax"


class TestRefreshCookieHardening:
    """The flags that keep the cookie unreadable and off plaintext links."""

    @override_settings(DEBUG=False, SESSION_COOKIE_SECURE=True)
    def test_production_cookie_is_secure_and_httponly(self):
        cookie = _refresh_cookie_settings()
        # httponly keeps XSS from reading the refresh token; secure keeps
        # it off any plaintext hop.
        assert cookie["httponly"] is True
        assert cookie["secure"] is True

    @override_settings(DEBUG=True, SESSION_COOKIE_SECURE=True)
    def test_dev_relaxes_secure_but_not_httponly(self):
        # Vite proxies over http locally, so Secure would drop the cookie
        # and bounce developers to /login. httponly is not negotiable.
        cookie = _refresh_cookie_settings()
        assert cookie["secure"] is False
        assert cookie["httponly"] is True

    @pytest.mark.parametrize("debug", [True, False])
    def test_name_path_and_lifetime_match_the_client(self, debug):
        # ftb-ui posts to /api/v1/auth/refresh/ with an empty body and
        # withCredentials, so the cookie must be readable across the whole
        # path. The 7-day max_age matches SIMPLE_JWT REFRESH_TOKEN_LIFETIME.
        with override_settings(DEBUG=debug):
            cookie = _refresh_cookie_settings()
        assert cookie["key"] == "refresh_token"
        assert cookie["path"] == "/"
        assert cookie["max_age"] == 7 * 24 * 60 * 60


class TestSettingsAreReadAtCallTime:
    """The regression this class now exists for.

    These attributes used to be frozen into a module-level
    REFRESH_COOKIE_SETTINGS at import, keyed only on DEBUG, so no setting
    could reach them. That made a plaintext deployment impossible to
    configure: the cookie stayed Secure, the browser silently discarded it
    over http://, and login returned 200 with the session gone on the next
    request -- nothing raised, nothing logged.
    """

    def test_the_frozen_constant_is_gone(self):
        from apps.accounts.api.v1 import views

        assert not hasattr(views, "REFRESH_COOKIE_SETTINGS")

    @override_settings(DEBUG=False, SESSION_COOKIE_SECURE=True)
    def test_tls_deployment_sets_secure(self):
        assert _refresh_cookie_settings()["secure"] is True

    @override_settings(DEBUG=False, SESSION_COOKIE_SECURE=False)
    def test_plaintext_deployment_does_not(self):
        assert _refresh_cookie_settings()["secure"] is False

    def test_two_calls_under_different_settings_differ(self):
        # Proves the value is not memoised anywhere. A lazily-recomputed
        # mapping would pass the two tests above and still fail this one.
        with override_settings(DEBUG=False, SESSION_COOKIE_SECURE=True):
            secure = _refresh_cookie_settings()["secure"]
        with override_settings(DEBUG=False, SESSION_COOKIE_SECURE=False):
            plain = _refresh_cookie_settings()["secure"]
        assert (secure, plain) == (True, False)


class TestInvariantsAcrossEveryMode:
    """What the scheme switch must never be able to reach."""

    @pytest.mark.parametrize("debug", [True, False])
    @pytest.mark.parametrize("cookie_secure", [True, False])
    def test_samesite_and_httponly_never_move(self, debug, cookie_secure):
        # SameSite=Lax is the only CSRF control on the cookie-reading
        # endpoints, and httponly is what keeps XSS off the refresh token.
        # Neither may follow PUBLIC_SCHEME.
        with override_settings(DEBUG=debug, SESSION_COOKIE_SECURE=cookie_secure):
            cookie = _refresh_cookie_settings()
        assert cookie["samesite"] == "Lax"
        assert cookie["httponly"] is True
