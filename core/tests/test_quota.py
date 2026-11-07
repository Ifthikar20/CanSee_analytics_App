"""
Daily per-user call-budget primitive backing the GEO + CSE services.
"""
import pytest

from core.quota import DailyQuota


@pytest.fixture
def quota():
    return DailyQuota(
        namespace="test",
        setting_name="UNIT_TEST_DAILY_LIMIT",
        default_limit=3,
    )


@pytest.mark.django_db
def test_limit_falls_back_to_default_when_setting_missing(settings, quota):
    if hasattr(settings, "UNIT_TEST_DAILY_LIMIT"):
        del settings.UNIT_TEST_DAILY_LIMIT
    assert quota.limit(user_id=1) == 3


@pytest.mark.django_db
def test_limit_reads_setting_when_present(settings, quota):
    settings.UNIT_TEST_DAILY_LIMIT = 7
    assert quota.limit(user_id=1) == 7


@pytest.mark.django_db
def test_remaining_starts_at_full_limit(settings, quota):
    settings.UNIT_TEST_DAILY_LIMIT = 5
    assert quota.remaining(user_id=42) == 5


@pytest.mark.django_db
def test_consume_decrements_remaining(settings, quota):
    settings.UNIT_TEST_DAILY_LIMIT = 5
    assert quota.consume(user_id=1) is True
    assert quota.consume(user_id=1) is True
    assert quota.remaining(user_id=1) == 3


@pytest.mark.django_db
def test_consume_refuses_beyond_limit_and_does_not_increment(settings, quota):
    settings.UNIT_TEST_DAILY_LIMIT = 2
    assert quota.consume(user_id=9) is True
    assert quota.consume(user_id=9) is True
    assert quota.consume(user_id=9) is False
    # The refused call must NOT have ticked the counter forward.
    assert quota.remaining(user_id=9) == 0


@pytest.mark.django_db
def test_buckets_are_per_user(settings, quota):
    settings.UNIT_TEST_DAILY_LIMIT = 1
    assert quota.consume(user_id=1) is True
    assert quota.consume(user_id=1) is False
    # User 2 has their own budget.
    assert quota.consume(user_id=2) is True


@pytest.mark.django_db
def test_anonymous_users_share_the_anon_bucket(settings, quota):
    settings.UNIT_TEST_DAILY_LIMIT = 1
    assert quota.consume(user_id=None) is True
    assert quota.consume(user_id=None) is False
    assert quota.consume(user_id=0) is False  # 0/None/"" all collapse to "anon"


@pytest.mark.django_db
def test_namespaces_do_not_collide(settings):
    settings.UNIT_TEST_DAILY_LIMIT = 1
    a = DailyQuota(namespace="a", setting_name="UNIT_TEST_DAILY_LIMIT", default_limit=1)
    b = DailyQuota(namespace="b", setting_name="UNIT_TEST_DAILY_LIMIT", default_limit=1)
    assert a.consume(user_id=1) is True
    # Same user, same limit, different namespace → independent bucket.
    assert b.consume(user_id=1) is True
