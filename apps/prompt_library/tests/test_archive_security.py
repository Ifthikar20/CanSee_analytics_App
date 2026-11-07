"""Security tests for prompt archiving (IDOR fix).

Archiving a saved prompt must be a PER-WEBSITE, tenant-scoped operation
on the caller's own BrandPrompt row — never a flip of the shared catalog
Prompt.is_active, which a previous design exposed to every authenticated
user and which bled across tenants.
"""
from __future__ import annotations

from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.tests.factories import UserFactory
from apps.prompt_library.models import BrandPrompt
from apps.prompt_library.tests.factories import PromptFactory
from apps.websites.tests.factories import WebsiteFactory


def _toggle_url(prompt_id, action):
    return f"/api/v1/prompt-library/prompts/{prompt_id}/{action}/"


def _bp_url(bp_id):
    return f"/api/v1/prompt-library/brand-prompts/{bp_id}/"


class TestGlobalToggleIsStaffOnly(TestCase):
    def setUp(self):
        self.user = UserFactory()  # non-staff
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.prompt = PromptFactory()

    def test_non_staff_cannot_flip_catalog_prompt(self):
        # The shared-catalog toggle is staff-only now; a normal user
        # must not be able to disable a prompt for every tenant.
        resp = self.client.post(_toggle_url(self.prompt.id, "disable"))
        assert resp.status_code == 403
        self.prompt.refresh_from_db()
        assert self.prompt.is_active is True


class TestPerWebsiteArchive(TestCase):
    def setUp(self):
        self.owner = UserFactory()
        self.website = WebsiteFactory(user=self.owner)
        self.prompt = PromptFactory()
        self.bp = BrandPrompt.objects.create(website=self.website, prompt=self.prompt)
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def test_owner_can_archive_without_touching_catalog(self):
        resp = self.client.patch(_bp_url(self.bp.id), {"is_archived": True}, format="json")
        assert resp.status_code == 200
        self.bp.refresh_from_db()
        self.prompt.refresh_from_db()
        assert self.bp.is_archived is True
        # The shared catalog row is untouched — no cross-tenant effect.
        assert self.prompt.is_active is True

    def test_other_tenant_cannot_archive(self):
        stranger = UserFactory()
        client = APIClient()
        client.force_authenticate(stranger)
        resp = client.patch(_bp_url(self.bp.id), {"is_archived": True}, format="json")
        assert resp.status_code == 404
        self.bp.refresh_from_db()
        assert self.bp.is_archived is False
