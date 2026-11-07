"""Tests for the retention pruners.

These guard a published promise: /what-we-track tells customers that old
analytics rows are purged on a schedule. Before prune_analytics_events
existed that was untrue -- only a read clamp was enforced -- so these tests
exist to keep the claim honest.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.analytics.models import AnalyticsAccessLog, PageEvent, Session, Visitor
from apps.analytics.tasks import prune_analytics_events
from apps.websites.models import Website
from core.utils.retention import (
    GEMINI_PROVIDER,
    effective_llm_result_retention_days,
    grounded_links_may_be_stored,
)


def _user(email="owner@cansee.ai"):
    from apps.accounts.models import User

    return User.objects.create_user(
        email=email, password="TestPass123!", full_name="Owner"
    )


def _website(user):
    return Website.objects.create(
        user=user,
        name="Outfi",
        url="https://www.outfi.ai",
        pixel_key=uuid.uuid4(),
        is_active=True,
    )


def _visitor(website, *, last_seen, fingerprint="fp"):
    v = Visitor.objects.create(website=website, fingerprint_hash=fingerprint)
    # last_seen is auto_now, so it has to be written back explicitly.
    Visitor.objects.filter(pk=v.pk).update(last_seen=last_seen)
    v.refresh_from_db()
    return v


@override_settings(ANALYTICS_RETENTION_DAYS=180, ACCESS_LOG_RETENTION_DAYS=90)
class PruneAnalyticsEventsTest(TestCase):
    def setUp(self):
        self.user = _user()
        self.website = _website(self.user)
        self.now = timezone.now()

    def _event(self, *, days_ago, visitor):
        ts = self.now - timedelta(days=days_ago)
        return PageEvent.objects.create(
            website=self.website,
            visitor=visitor,
            event_type="pageview",
            url="https://www.outfi.ai/",
            timestamp=ts,
        )

    def test_deletes_only_rows_outside_the_window(self):
        recent_visitor = _visitor(
            self.website, last_seen=self.now - timedelta(days=1), fingerprint="recent"
        )
        old_visitor = _visitor(
            self.website, last_seen=self.now - timedelta(days=400), fingerprint="old"
        )
        keep = self._event(days_ago=10, visitor=recent_visitor)
        drop = self._event(days_ago=200, visitor=recent_visitor)

        result = prune_analytics_events()

        self.assertTrue(PageEvent.objects.filter(pk=keep.pk).exists())
        self.assertFalse(PageEvent.objects.filter(pk=drop.pk).exists())
        self.assertTrue(Visitor.objects.filter(pk=recent_visitor.pk).exists())
        self.assertFalse(Visitor.objects.filter(pk=old_visitor.pk).exists())
        self.assertEqual(result["page_events"], 1)
        self.assertEqual(result["visitors"], 1)

    def test_boundary_row_is_kept(self):
        """A row exactly inside the window must survive."""
        visitor = _visitor(self.website, last_seen=self.now)
        edge = self._event(days_ago=179, visitor=visitor)

        prune_analytics_events()

        self.assertTrue(PageEvent.objects.filter(pk=edge.pk).exists())

    def test_access_log_uses_its_own_shorter_window(self):
        """90 days for the security trail, not the 180 used for events."""
        keep = AnalyticsAccessLog.objects.create(
            website_id_raw=str(self.website.id), path="/api/v1/analytics/x/"
        )
        drop = AnalyticsAccessLog.objects.create(
            website_id_raw=str(self.website.id), path="/api/v1/analytics/y/"
        )
        # accessed_at is auto_now_add.
        AnalyticsAccessLog.objects.filter(pk=keep.pk).update(
            accessed_at=self.now - timedelta(days=100 - 20)
        )
        AnalyticsAccessLog.objects.filter(pk=drop.pk).update(
            accessed_at=self.now - timedelta(days=120)
        )

        prune_analytics_events()

        self.assertTrue(AnalyticsAccessLog.objects.filter(pk=keep.pk).exists())
        self.assertFalse(AnalyticsAccessLog.objects.filter(pk=drop.pk).exists())

    def test_dry_run_counts_without_deleting(self):
        visitor = _visitor(self.website, last_seen=self.now)
        self._event(days_ago=300, visitor=visitor)

        result = prune_analytics_events(dry_run=True)

        self.assertTrue(result["dry_run"])
        self.assertEqual(result["page_events"], 1)
        self.assertEqual(PageEvent.objects.count(), 1)

    def test_sessions_are_pruned_on_their_own_timestamp(self):
        visitor = _visitor(self.website, last_seen=self.now)
        old = Session.objects.create(
            visitor=visitor,
            started_at=self.now - timedelta(days=200),
        )
        new = Session.objects.create(
            visitor=visitor,
            started_at=self.now - timedelta(days=5),
        )

        prune_analytics_events()

        self.assertFalse(Session.objects.filter(pk=old.pk).exists())
        self.assertTrue(Session.objects.filter(pk=new.pk).exists())


class GeminiGroundingRetentionCapTest(TestCase):
    """The cap that keeps us inside Gemini's Grounded Results terms."""

    @override_settings(LLM_RESULT_RETENTION_DAYS=730, LLM_WEBSEARCH_ENABLED=False)
    def test_ungrounded_gemini_uses_the_normal_window(self):
        self.assertEqual(effective_llm_result_retention_days(GEMINI_PROVIDER), 730)
        self.assertTrue(grounded_links_may_be_stored(GEMINI_PROVIDER))

    @override_settings(
        LLM_RESULT_RETENTION_DAYS=730,
        LLM_WEBSEARCH_ENABLED=True,
        GROUNDED_RESULT_MAX_RETENTION_DAYS=30,
    )
    def test_grounded_gemini_collapses_to_thirty_days(self):
        self.assertEqual(effective_llm_result_retention_days(GEMINI_PROVIDER), 30)
        self.assertFalse(grounded_links_may_be_stored(GEMINI_PROVIDER))

    @override_settings(LLM_RESULT_RETENTION_DAYS=730, LLM_WEBSEARCH_ENABLED=True)
    def test_other_providers_are_unaffected_by_the_cap(self):
        for provider in ("claude", "gpt4", "perplexity", "grok"):
            with self.subTest(provider=provider):
                self.assertEqual(effective_llm_result_retention_days(provider), 730)
                self.assertTrue(grounded_links_may_be_stored(provider))
