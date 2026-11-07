"""Tests for the GEO notification producers."""
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.llm_ranking.models import LLMRankingAudit
from apps.llm_ranking.tests.factories import (
    LLMRankingAuditFactory,
    LLMRankingResultFactory,
)
from apps.notifications.models import Notification
from apps.notifications.services.geo_events import (
    VISIBILITY_DELTA_THRESHOLD,
    record_audit_events,
)


def _completed(website, user, *, mention_rate, completed_at, overall_score=70):
    return LLMRankingAuditFactory(
        website=website,
        created_by=user,
        status=LLMRankingAudit.STATUS_COMPLETED,
        mention_rate=mention_rate,
        overall_score=overall_score,
        completed_at=completed_at,
    )


@pytest.mark.django_db
class TestRecordAuditEvents:
    def test_first_audit_emits_only_completion(self):
        audit = _completed(
            website=LLMRankingAuditFactory().website,
            user=LLMRankingAuditFactory().created_by,
            mention_rate=42.0,
            completed_at=timezone.now(),
        )
        with patch("apps.notifications.services.notification_service.channel_layer"):
            record_audit_events(audit)
        types = list(Notification.objects.filter(user=audit.created_by).values_list("type", flat=True))
        assert types == ["audit_complete"]

    def test_large_visibility_jump_emits_milestone(self):
        prev_audit = LLMRankingAuditFactory(
            status=LLMRankingAudit.STATUS_COMPLETED,
            mention_rate=20.0,
            completed_at=timezone.now() - timezone.timedelta(days=1),
        )
        audit = _completed(
            website=prev_audit.website,
            user=prev_audit.created_by,
            mention_rate=20.0 + VISIBILITY_DELTA_THRESHOLD + 1,
            completed_at=timezone.now(),
        )
        with patch("apps.notifications.services.notification_service.channel_layer"):
            record_audit_events(audit)
        types = set(Notification.objects.filter(user=audit.created_by).values_list("type", flat=True))
        assert "milestone" in types
        assert "audit_complete" in types

    def test_visibility_drop_emits_competitor_alert(self):
        prev_audit = LLMRankingAuditFactory(
            status=LLMRankingAudit.STATUS_COMPLETED,
            mention_rate=60.0,
            completed_at=timezone.now() - timezone.timedelta(days=1),
        )
        audit = _completed(
            website=prev_audit.website,
            user=prev_audit.created_by,
            mention_rate=60.0 - VISIBILITY_DELTA_THRESHOLD - 1,
            completed_at=timezone.now(),
        )
        with patch("apps.notifications.services.notification_service.channel_layer"):
            record_audit_events(audit)
        notes = Notification.objects.filter(user=audit.created_by).values_list("type", "title")
        assert any(t == "competitor_alert" and "down" in title.lower() for t, title in notes)

    def test_small_visibility_change_does_not_emit(self):
        prev_audit = LLMRankingAuditFactory(
            status=LLMRankingAudit.STATUS_COMPLETED,
            mention_rate=50.0,
            completed_at=timezone.now() - timezone.timedelta(days=1),
        )
        audit = _completed(
            website=prev_audit.website,
            user=prev_audit.created_by,
            mention_rate=51.0,
            completed_at=timezone.now(),
        )
        with patch("apps.notifications.services.notification_service.channel_layer"):
            record_audit_events(audit)
        types = list(Notification.objects.filter(user=audit.created_by).values_list("type", flat=True))
        assert types == ["audit_complete"]

    def test_new_competitor_emits_alert(self):
        prev_audit = LLMRankingAuditFactory(
            status=LLMRankingAudit.STATUS_COMPLETED,
            mention_rate=30.0,
            completed_at=timezone.now() - timezone.timedelta(days=1),
        )
        LLMRankingResultFactory(audit=prev_audit, competitors_mentioned=["OldRival"])
        audit = _completed(
            website=prev_audit.website,
            user=prev_audit.created_by,
            mention_rate=30.0,
            completed_at=timezone.now(),
        )
        LLMRankingResultFactory(audit=audit, competitors_mentioned=["OldRival", "NewChallenger"])
        with patch("apps.notifications.services.notification_service.channel_layer"):
            record_audit_events(audit)
        alerts = Notification.objects.filter(user=audit.created_by, type="competitor_alert")
        assert alerts.exists()
        assert any("NewChallenger" in n.message for n in alerts)

    def test_prompt_coverage_milestone(self):
        audit = _completed(
            website=LLMRankingAuditFactory().website,
            user=LLMRankingAuditFactory().created_by,
            mention_rate=10.0,
            completed_at=timezone.now(),
        )
        # Distinct prompt_index per row: (audit, prompt_index, provider,
        # run_id) is unique, so 50 factory rows on default index 0 collide.
        for i in range(50):
            LLMRankingResultFactory(audit=audit, prompt_index=i)
        with patch("apps.notifications.services.notification_service.channel_layer"):
            record_audit_events(audit)
        milestones = Notification.objects.filter(user=audit.created_by, type="milestone")
        assert milestones.exists()
        assert any("50" in n.title for n in milestones)
