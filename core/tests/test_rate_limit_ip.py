"""NET-01: the throttle key must be proxy-aware, not the spoofable left-most
X-Forwarded-For hop.

nginx appends the real client on the right, so the left-most entry is whatever
the client claimed. Keying the throttle on it let an attacker rotate the header
for a fresh bucket every request. The middleware now delegates to the shared
proxy-aware helper, which reads from the right per TRUSTED_PROXY_COUNT.
"""

from django.test import RequestFactory, override_settings

from core.middleware.rate_limit import AdaptiveRateLimitMiddleware


def _mw():
    return AdaptiveRateLimitMiddleware(lambda r: None)


@override_settings(TRUSTED_PROXY_COUNT=1)
def test_left_most_xff_does_not_control_key():
    req = RequestFactory().get(
        "/api/v1/auth/login",
        HTTP_X_FORWARDED_FOR="9.9.9.9, 203.0.113.7, 10.0.0.1",
        REMOTE_ADDR="10.0.0.1",
    )
    ip = _mw()._get_client_ip(req)
    assert ip != "9.9.9.9"        # the forged left-most no longer wins
    assert ip == "203.0.113.7"    # the entry just left of the trusted hop


@override_settings(TRUSTED_PROXY_COUNT=1)
def test_rotating_the_left_prefix_yields_the_same_key():
    mw = _mw()
    tail = "203.0.113.7, 10.0.0.1"  # real client + our nginx hop
    keys = {
        mw._get_client_ip(
            RequestFactory().get("/api/v1/auth/login", HTTP_X_FORWARDED_FOR=prefix + tail)
        )
        for prefix in ("", "1.1.1.1, ", "2.2.2.2, ", "1.1.1.1, 2.2.2.2, ")
    }
    # However many fake hops the attacker prepends, the key is unchanged.
    assert keys == {"203.0.113.7"}
