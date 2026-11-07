"""The admin mount path is configurable, and normalises whatever it is given.

Moving Django admin off "/admin/" is noise reduction, not access control:
/admin/ is one of the most scanned paths on the internet, and with
django-axes locking an account after five failures, a bot spraying `admin`
at the default path can lock out a real user. These tests pin that the
setting actually moves the mount, and that a fat-fingered value (leading
slash, missing trailing slash) still produces a working path rather than a
500 at import.
"""
import pytest
from django.conf import settings


@pytest.mark.parametrize(
    "given,expected",
    [
        ("admin", "admin/"),
        ("admin/", "admin/"),
        ("/admin", "admin/"),
        ("/admin/", "admin/"),
        ("  manage-a8f3d21b  ", "manage-a8f3d21b/"),
        ("ops/console", "ops/console/"),
    ],
)
def test_admin_url_is_normalised(given, expected):
    # Mirrors the expression in base.py. Django's path() rejects a leading
    # slash and needs a trailing one, so an operator writing "/admin" in an
    # env file would otherwise crash the app at startup.
    assert given.strip().strip("/") + "/" == expected


def test_default_is_unchanged_for_dev_and_tests():
    # Nothing about local development or the suite should shift because
    # production moves the path.
    assert settings.ADMIN_URL == "admin/"


def test_admin_is_mounted_where_the_setting_says():
    from django.urls import get_resolver

    patterns = {str(p.pattern) for p in get_resolver().url_patterns}
    assert settings.ADMIN_URL in patterns


def test_admin_path_is_not_hardcoded_in_urlconf():
    # The regression guard: someone "tidying" config/urls.py back to a
    # literal "admin/" would silently undo this on the next deploy.
    from pathlib import Path

    urlconf = (Path(__file__).resolve().parents[2] / "config" / "urls.py").read_text(
        encoding="utf-8"
    )
    code = "\n".join(
        line for line in urlconf.splitlines() if not line.lstrip().startswith("#")
    )
    assert 'path("admin/"' not in code
    assert "settings.ADMIN_URL" in code
