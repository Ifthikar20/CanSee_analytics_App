"""Account deletion — GDPR Article 17 erasure.

delete_account was previously a soft-delete that anonymized the User row
and kept every owned row (websites, visitors, chunks, subscription) in
place. These tests pin the replacement: the entire cascade tree is gone,
the vector index is asked to drop each website's collections, a managed
Polar subscription is cancelled, axes rows keyed by the email string are
purged — and provider failures never block erasure.
"""
import uuid
from unittest.mock import MagicMock, patch

import pytest
from rest_framework.test import APIClient

from apps.accounts.models import LoginAttempt, User
from apps.accounts.services.user_service import UserService
from apps.billing.models import Subscription
from apps.metering.models import PolarEventOutbox
from apps.rag.models import KnowledgeChunk, KnowledgeSource
from apps.websites.models import Website
from core.utils.constants import Plan, SubscriptionStatus


def _user_with_everything():
    """A user owning one of each major row type."""
    user = User.objects.create_user(
        email=f"erase-{uuid.uuid4().hex[:8]}@example.com",
        password="TestPass123!", full_name="Erase Me",
    )
    website = Website.objects.create(
        user=user, name="EraseCo", url="https://eraseco.example.com",
        industry="SaaS", pixel_key=uuid.uuid4(), is_active=True,
    )
    source = KnowledgeSource.objects.create(
        user=user, website=website, url="https://eraseco.example.com/page",
        kind=KnowledgeSource.KIND_DOCS, status=KnowledgeSource.STATUS_READY,
    )
    KnowledgeChunk.objects.create(
        source=source, user=user, website=website, chunk_index=0,
        text="content that must not survive", embedding=[0.1] * 8,
        embedding_model="hash-256", embedding_dim=8,
    )
    Subscription.objects.create(
        user=user, plan=Plan.INDIVIDUAL, status=SubscriptionStatus.ACTIVE,
    )
    return user, website


@pytest.mark.django_db
class TestDeleteAccountService:
    def test_every_owned_row_is_gone(self):
        user, website = _user_with_everything()
        uid, wid = user.id, website.id

        UserService.delete_account(user=user)

        assert not User.objects.filter(id=uid).exists()
        assert not Website.objects.filter(id=wid).exists()
        assert not KnowledgeSource.objects.filter(user_id=uid).exists()
        assert not KnowledgeChunk.objects.filter(user_id=uid).exists()
        assert not Subscription.objects.filter(user_id=uid).exists()

    def test_no_anonymized_husk_remains(self):
        """The old implementation left a deleted_<id>@deleted.invalid row.
        Erasure means no row at all."""
        user, _ = _user_with_everything()
        UserService.delete_account(user=user)
        assert not User.objects.filter(email__endswith="deleted.invalid").exists()

    def test_vector_index_is_asked_to_drop_each_website(self):
        user, website = _user_with_everything()
        backend = MagicMock()
        with patch(
            "apps.rag.services.vector_backends.get_backend", return_value=backend,
        ):
            UserService.delete_account(user=user)
        backend.delete_website.assert_called_once_with(website_id=website.id)

    def test_managed_polar_subscription_is_cancelled(self):
        user, _ = _user_with_everything()
        sub = user.subscription
        sub.polar_subscription_id = "polar_sub_erase"
        sub.save(update_fields=["polar_subscription_id"])

        with patch(
            "apps.billing.services.polar_billing.set_cancel_at_period_end",
        ) as cancel:
            UserService.delete_account(user=user)
        cancel.assert_called_once()
        assert cancel.call_args.kwargs["cancel"] is True

    def test_provider_failure_never_blocks_erasure(self):
        """The right to erasure does not wait for a billing API."""
        user, _ = _user_with_everything()
        sub = user.subscription
        sub.polar_subscription_id = "polar_sub_erase"
        sub.save(update_fields=["polar_subscription_id"])
        uid = user.id

        with patch(
            "apps.billing.services.polar_billing.set_cancel_at_period_end",
            side_effect=RuntimeError("polar down"),
        ):
            UserService.delete_account(user=user)
        assert not User.objects.filter(id=uid).exists()

    def test_axes_rows_keyed_by_email_are_purged(self):
        from axes.models import AccessAttempt

        user, _ = _user_with_everything()
        email = user.email
        AccessAttempt.objects.create(
            username=email, ip_address="203.0.113.9",
            user_agent="test", failures_since_start=3,
        )
        UserService.delete_account(user=user)
        assert not AccessAttempt.objects.filter(username=email).exists()

    def test_login_attempt_rows_are_erased(self):
        """ERAS-01: accounts.LoginAttempt uses on_delete=SET_NULL and keeps
        email/IP/user-agent, so the cascade would leave it behind."""
        user, _ = _user_with_everything()
        email = user.email
        LoginAttempt.objects.create(
            email=email, ip_address="203.0.113.9", user_agent="ua",
            success=False, user=user,
        )
        UserService.delete_account(user=user)
        assert not LoginAttempt.objects.filter(email=email).exists()
        assert not LoginAttempt.objects.filter(user_id=None, email=email).exists()

    def test_polar_outbox_rows_are_erased(self):
        """ERAS-03: metering.PolarEventOutbox has no user FK and carries
        external_customer_id = str(user.id) plus a usage payload."""
        user, _ = _user_with_everything()
        uid = str(user.id)
        PolarEventOutbox.objects.create(
            idempotency_key=f"idem-{uuid.uuid4().hex}",
            external_customer_id=uid, payload={"cost": 1},
        )
        UserService.delete_account(user=user)
        assert not PolarEventOutbox.objects.filter(external_customer_id=uid).exists()


@pytest.mark.django_db
class TestDeleteEndpoint:
    def test_authenticated_delete_erases_and_returns_200(self):
        user, _ = _user_with_everything()
        uid = user.id
        client = APIClient()
        client.force_authenticate(user=user)

        resp = client.delete("/api/v1/auth/me/")

        assert resp.status_code == 200
        assert not User.objects.filter(id=uid).exists()

    def test_unauthenticated_is_rejected(self):
        assert APIClient().delete("/api/v1/auth/me/").status_code == 401
