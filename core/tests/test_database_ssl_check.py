"""The deploy check that stops Postgres being reached without TLS.

``sslmode=prefer`` silently falls back to an unencrypted connection when
the server does not offer TLS, and nothing downstream can tell which it
got. These tests pin the one rule that matters: off loopback, a
fallback-capable sslmode is a deploy error.
"""
import pytest
from django.test import override_settings

from core.checks import check_database_ssl

# check_database_ssl only reads settings.DATABASES -- it never opens a
# connection -- so Django's "Overriding setting DATABASES" warning does not
# apply here and would otherwise be 30 lines of noise per run.
pytestmark = pytest.mark.filterwarnings(
    "ignore:Overriding setting DATABASES.*:UserWarning"
)

_PG = "django.db.backends.postgresql"


def _databases(host, sslmode=None, engine=_PG):
    options = {"connect_timeout": 10}
    if sslmode is not None:
        options["sslmode"] = sslmode
    return {"default": {"ENGINE": engine, "HOST": host, "OPTIONS": options}}


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1", "", "LOCALHOST"])
@pytest.mark.parametrize("sslmode", ["prefer", "disable", "allow"])
def test_loopback_is_allowed_without_tls(host, sslmode):
    """Traffic that never leaves the machine needs no transport encryption."""
    with override_settings(DATABASES=_databases(host, sslmode)):
        assert check_database_ssl(None) == []


@pytest.mark.parametrize("host", ["db", "postgres", "cansee-db"])
def test_container_service_names_are_private(host):
    """DB_HOST=db is a Compose/K8s service name on a private bridge.

    Without this the check fired on every Docker deployment, which is the
    project's actual production topology.
    """
    with override_settings(DATABASES=_databases(host, "prefer"), DB_TRUSTED_HOSTS=[]):
        assert check_database_ssl(None) == []


def test_unix_socket_path_is_private():
    with override_settings(DATABASES=_databases("/var/run/postgresql", "prefer")):
        assert check_database_ssl(None) == []


def test_dotted_host_can_be_trusted_explicitly():
    """The escape hatch for a private network that uses dotted names."""
    host = "pg.internal.vpc"
    with override_settings(DATABASES=_databases(host, "prefer"), DB_TRUSTED_HOSTS=[]):
        assert len(check_database_ssl(None)) == 1
    with override_settings(DATABASES=_databases(host, "prefer"), DB_TRUSTED_HOSTS=[host]):
        assert check_database_ssl(None) == []


@pytest.mark.parametrize("sslmode", ["disable", "allow", "prefer"])
def test_remote_host_with_fallback_sslmode_is_an_error(sslmode):
    with override_settings(DATABASES=_databases("db.example.rds.amazonaws.com", sslmode)):
        errors = check_database_ssl(None)
    assert len(errors) == 1
    assert errors[0].id == "core.E003"
    assert sslmode in errors[0].msg


@pytest.mark.parametrize("sslmode", ["require", "verify-ca", "verify-full"])
def test_remote_host_with_enforced_tls_passes(sslmode):
    with override_settings(DATABASES=_databases("db.example.rds.amazonaws.com", sslmode)):
        assert check_database_ssl(None) == []


def test_remote_host_with_no_sslmode_at_all_is_an_error():
    """An absent sslmode is libpq's 'prefer' -- the same silent fallback."""
    with override_settings(DATABASES=_databases("db.example.rds.amazonaws.com")):
        errors = check_database_ssl(None)
    assert len(errors) == 1
    assert errors[0].id == "core.E003"


def test_sslmode_comparison_is_case_insensitive():
    with override_settings(DATABASES=_databases("db.example.com", "PREFER")):
        assert len(check_database_ssl(None)) == 1


def test_non_postgres_backend_is_skipped():
    """sslmode is a libpq concept; other engines are out of scope."""
    with override_settings(
        DATABASES=_databases("db.example.com", "prefer", engine="django.db.backends.sqlite3")
    ):
        assert check_database_ssl(None) == []
