"""The deploy check that stops a real domain being served over plaintext.

PUBLIC_SCHEME=http disables the Secure flag on every cookie, the SSL
redirect, and HSTS. That is correct for a host reached by bare IP, where
no certificate authority will issue a certificate, and wrong everywhere
else -- but the wrongness is invisible: the site renders, requests
succeed, and only a packet capture shows the session cookie in the clear.

These tests pin the three ways that configuration can be wrong.
"""
import pytest
from django.test import override_settings

from core.checks import _is_public_domain, check_public_scheme

# A coherent plaintext deployment: bare IP, everything derived correctly.
_HTTP_BASELINE = dict(
    DEBUG=False,
    PUBLIC_SCHEME="http",
    ALLOWED_HOSTS=["100.31.135.211"],
    SECURE_SSL_REDIRECT=False,
    SECURE_PROXY_SSL_HEADER=None,
    SESSION_COOKIE_SECURE=False,
    CSRF_COOKIE_SECURE=False,
    CSRF_TRUSTED_ORIGINS=["http://100.31.135.211"],
    CORS_ALLOWED_ORIGINS=[],
    FRONTEND_URL="http://100.31.135.211",
)


def _http(**overrides):
    return {**_HTTP_BASELINE, **overrides}


def _ids(errors):
    return sorted(e.id for e in errors)


class TestShortCircuits:
    def test_https_is_never_flagged(self):
        with override_settings(PUBLIC_SCHEME="https", ALLOWED_HOSTS=["cansee.ai"]):
            assert check_public_scheme(None) == []

    def test_debug_is_never_flagged(self):
        # Local development is not a deployment.
        with override_settings(**_http(DEBUG=True, ALLOWED_HOSTS=["cansee.ai"])):
            assert check_public_scheme(None) == []


class TestLegitimatePlaintextDeployment:
    """The most important test here.

    A correct bare-IP deployment must pass cleanly. If it does not,
    operators learn to run with --skip-checks and every other check in
    this module stops being worth anything.
    """

    def test_clean_ip_config_passes(self):
        with override_settings(**_HTTP_BASELINE):
            assert check_public_scheme(None) == []

    @pytest.mark.parametrize(
        "host",
        ["100.31.135.211", "127.0.0.1", "::1", "[::1]", "localhost", "web", "db",
         "box.internal", "dev.local", "*"],
    )
    def test_non_domain_hosts_are_fine(self, host):
        with override_settings(**_http(ALLOWED_HOSTS=[host])):
            assert _ids(check_public_scheme(None)) == []


class TestDomainOverPlaintext:
    """core.E004 -- the .env-copied-to-the-wrong-box accident."""

    @pytest.mark.parametrize("host", ["cansee.ai", "www.cansee.ai", ".cansee.ai", "API.CANSEE.AI"])
    def test_domain_in_allowed_hosts_errors(self, host):
        with override_settings(**_http(ALLOWED_HOSTS=[host])):
            errors = check_public_scheme(None)
        assert _ids(errors) == ["core.E004"]

    def test_the_message_names_only_the_domain(self):
        with override_settings(
            **_http(ALLOWED_HOSTS=["100.31.135.211", "cansee.ai", "localhost"])
        ):
            errors = check_public_scheme(None)
        assert len(errors) == 1
        assert "cansee.ai" in errors[0].msg
        assert "100.31.135.211" not in errors[0].msg


class TestContradictorySettings:
    """core.E005 -- a settings module re-set a derived value."""

    @pytest.mark.parametrize(
        "override",
        [
            {"SECURE_SSL_REDIRECT": True},
            {"SECURE_PROXY_SSL_HEADER": ("HTTP_X_FORWARDED_PROTO", "https")},
            {"SESSION_COOKIE_SECURE": True},
            {"CSRF_COOKIE_SECURE": True},
        ],
    )
    def test_each_contradiction_alone_errors(self, override):
        with override_settings(**_http(**override)):
            errors = check_public_scheme(None)
        assert "core.E005" in _ids(errors)
        assert next(iter(override)) in errors[0].msg

    def test_all_four_produce_one_error_not_four(self):
        with override_settings(
            **_http(
                SECURE_SSL_REDIRECT=True,
                SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
                SESSION_COOKIE_SECURE=True,
                CSRF_COOKIE_SECURE=True,
            )
        ):
            errors = check_public_scheme(None)
        assert _ids(errors) == ["core.E005"]


class TestLeftoverHttpsOrigins:
    """core.E006 -- trust lists that can never match a plaintext request."""

    @pytest.mark.parametrize(
        "override",
        [
            {"CSRF_TRUSTED_ORIGINS": ["https://cansee.ai"]},
            {"CORS_ALLOWED_ORIGINS": ["https://cansee.ai"]},
            {"FRONTEND_URL": "https://cansee.ai"},
        ],
    )
    def test_https_leftovers_error(self, override):
        with override_settings(**_http(**override)):
            assert "core.E006" in _ids(check_public_scheme(None))

    def test_real_env_prod_drift_is_caught(self):
        # The exact shape .env.prod was in when this was written: host list
        # never updated, FRONTEND_URL still pointing at the domain.
        with override_settings(
            **_http(
                ALLOWED_HOSTS=["localhost", "127.0.0.1"],
                CSRF_TRUSTED_ORIGINS=["http://localhost"],
                FRONTEND_URL="https://cansee.ai",
            )
        ):
            assert _ids(check_public_scheme(None)) == ["core.E006"]


class TestNoIdCollisions:
    def test_everything_wrong_reports_all_three(self):
        with override_settings(
            **_http(
                ALLOWED_HOSTS=["cansee.ai"],
                SECURE_SSL_REDIRECT=True,
                FRONTEND_URL="https://cansee.ai",
            )
        ):
            errors = check_public_scheme(None)
        assert _ids(errors) == ["core.E004", "core.E005", "core.E006"]

    def test_does_not_reuse_existing_ids(self):
        with override_settings(**_http(ALLOWED_HOSTS=["cansee.ai"])):
            ids = {e.id for e in check_public_scheme(None)}
        assert ids.isdisjoint({"core.E001", "core.E002", "core.E003"})


class TestIsPublicDomain:
    @pytest.mark.parametrize(
        "host", ["cansee.ai", "www.cansee.ai", ".cansee.ai", "API.CANSEE.AI", "cansee.ai."]
    )
    def test_public(self, host):
        assert _is_public_domain(host) is True

    @pytest.mark.parametrize(
        "host",
        ["100.31.135.211", "::1", "[::1]", "localhost", "127.0.0.1", "*", "",
         "web", "db", "box.internal", "dev.local", "x.test", "testserver"],
    )
    def test_not_public(self, host):
        assert _is_public_domain(host) is False
