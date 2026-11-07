"""Tests for the unauthenticated build-identity endpoint."""
from __future__ import annotations

import pytest
from rest_framework.test import APIClient

URL = "/api/v1/version/"


@pytest.mark.django_db
def test_version_requires_no_auth():
    resp = APIClient().get(URL)
    assert resp.status_code == 200


@pytest.mark.django_db
def test_version_reports_dev_when_unset(settings):
    settings.GIT_SHA = ""
    settings.BUILD_NUMBER = ""
    settings.BUILD_TIME = ""
    data = APIClient().get(URL).json()["data"]
    assert data["label"] == "dev"
    assert data["version"] == "dev"
    assert data["build"] is None
    assert data["built_at"] is None


@pytest.mark.django_db
def test_version_reports_short_sha_and_build(settings):
    settings.GIT_SHA = "9195e6f252e7783f8ac04ceb8468654c408c43d5"
    settings.BUILD_NUMBER = "512"
    settings.BUILD_TIME = "2026-07-05T00:00:00Z"
    data = APIClient().get(URL).json()["data"]
    assert data["label"] == "v0705-512"
    assert data["version"] == "9195e6f"
    assert data["build"] == "512"
    assert data["built_at"] == "2026-07-05T00:00:00Z"
