import pytest
from django.core.management import call_command


@pytest.fixture(scope="session")
def django_db_setup(django_test_environment, django_db_blocker):
    """Ensure the test database has all migrations applied."""
    with django_db_blocker.unblock():
        call_command("migrate", "--run-syncdb", verbosity=0)


@pytest.fixture(autouse=True)
def reset_cache():
    """Clear cache between tests."""
    from django.core.cache import cache
    yield
    cache.clear()
