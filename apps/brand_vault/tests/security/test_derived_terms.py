"""Tests for knowledge-base-derived search terms."""
from __future__ import annotations

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.brand_vault.services.security.derived_terms import (
    derived_search_terms,
)
from apps.rag.models import KnowledgeSource
from apps.websites.tests.factories import WebsiteFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return UserFactory()


@pytest.fixture
def website(user):
    return WebsiteFactory(user=user, name="Acme")


def _source(user, website, title, *, status=KnowledgeSource.STATUS_READY,
            kind=KnowledgeSource.KIND_OTHER, url=None):
    return KnowledgeSource.objects.create(
        user=user, website=website,
        url=url or f"https://acme.io/{abs(hash(title))}",
        title=title, status=status, kind=kind,
    )


def test_extracts_distinctive_title_segments(user, website):
    _source(user, website, "Acme Widget Pro — Pricing")
    _source(user, website, "Acme | Blog")

    terms = derived_search_terms(website)
    assert "Acme Widget Pro" in terms
    # "Pricing" and "Blog" are generic navigation words.
    assert "Pricing" not in terms
    assert "Blog" not in terms


def test_excludes_brand_terms_and_generic_segments(user, website):
    _source(user, website, "Acme — About us")
    _source(user, website, "Refund policy")

    terms = derived_search_terms(website, exclude=["Acme"])
    assert terms == []


def test_dedupes_case_insensitively_and_caps_at_limit(user, website):
    _source(user, website, "WidgetPro Overview Guide")
    _source(user, website, "WIDGETPRO OVERVIEW GUIDE", url="https://acme.io/x")
    for i in range(10):
        _source(user, website, f"Distinct Feature Number {i}")

    terms = derived_search_terms(website, limit=3)
    assert len(terms) == 3
    lowered = [t.lower() for t in terms]
    assert len(set(lowered)) == 3


def test_ignores_non_ready_and_audit_context_sources(user, website):
    _source(user, website, "Pending Product Name",
            status=KnowledgeSource.STATUS_PENDING)
    _source(user, website, "Machine Notes",
            kind=KnowledgeSource.KIND_AUDIT_CONTEXT)

    assert derived_search_terms(website) == []


def test_drops_long_numeric_and_short_segments(user, website):
    _source(user, website, "A")
    _source(user, website, "2024")
    _source(user, website, "This title has far too many words to be a query")

    assert derived_search_terms(website) == []


def test_scoped_to_owning_user(user, website):
    other = UserFactory()
    KnowledgeSource.objects.create(
        user=other, website=website, url="https://acme.io/other",
        title="Other Users Product", status=KnowledgeSource.STATUS_READY,
    )
    assert derived_search_terms(website) == []
