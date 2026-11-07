"""Tests for per-prompt scheduling: the model, the REST endpoint, the
Beat dispatcher, and the run-history/time-series additions to the
detail + dashboard aggregation views.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.tests.factories import UserFactory
from apps.llm_ranking.tests.factories import (
    LLMRankingAuditFactory,
    LLMRankingResultFactory,
)
from apps.prompt_library.models import (
    BrandPrompt,
    PromptCrawlRun,
    PromptSchedule,
)
from apps.prompt_library.tasks import (
    PROMPT_FREQUENCY_DELTAS,
    _record_prompt_failure,
    dispatch_scheduled_prompt_scans,
)
from apps.prompt_library.tests.factories import PromptFactory
from apps.websites.tests.factories import WebsiteFactory

PREFIX = "/api/v1/prompt-library"


@pytest.fixture
def auth():
    user = UserFactory()
    website = WebsiteFactory(user=user)
    client = APIClient()
    client.force_authenticate(user=user)
    return client, user, website


def _data(resp):
    """Unwrap the EnvelopeRenderer's {"success", "data"} response shape."""
    return resp.json()["data"]


def _schedule_url(website, prompt):
    return f"{PREFIX}/websites/{website.id}/prompts/{prompt.id}/schedule/"


def _save(website, prompt):
    return BrandPrompt.objects.create(website=website, prompt=prompt)


# ── Endpoint ────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_schedule_requires_auth():
    website = WebsiteFactory()
    prompt = PromptFactory()
    anon = APIClient()
    assert anon.get(_schedule_url(website, prompt)).status_code == 401


@pytest.mark.django_db
def test_get_schedule_none_when_unset(auth):
    client, _, website = auth
    prompt = PromptFactory()
    _save(website, prompt)
    resp = client.get(_schedule_url(website, prompt))
    assert resp.status_code == 200
    assert _data(resp)["schedule"] is None


@pytest.mark.django_db
def test_post_creates_schedule(auth):
    client, user, website = auth
    prompt = PromptFactory()
    bp = _save(website, prompt)

    before = timezone.now()
    resp = client.post(
        _schedule_url(website, prompt), {"frequency": "daily"}, format="json",
    )
    assert resp.status_code == 201
    body = _data(resp)["schedule"]
    assert body["frequency"] == "daily"
    assert body["is_enabled"] is True

    sched = PromptSchedule.objects.get(brand_prompt=bp)
    assert sched.created_by_id == user.id
    # next_run_at is one cadence out (daily ~ +1 day).
    assert before + timedelta(hours=23) < sched.next_run_at < before + timedelta(hours=25)


@pytest.mark.django_db
def test_post_updates_existing_schedule(auth):
    client, _, website = auth
    prompt = PromptFactory()
    bp = _save(website, prompt)

    client.post(_schedule_url(website, prompt), {"frequency": "daily"}, format="json")
    resp = client.post(
        _schedule_url(website, prompt), {"frequency": "weekly"}, format="json",
    )
    assert resp.status_code == 200  # updated, not created
    assert PromptSchedule.objects.filter(brand_prompt=bp).count() == 1
    assert PromptSchedule.objects.get(brand_prompt=bp).frequency == "weekly"


@pytest.mark.django_db
def test_delete_removes_schedule(auth):
    client, _, website = auth
    prompt = PromptFactory()
    bp = _save(website, prompt)
    client.post(_schedule_url(website, prompt), {"frequency": "daily"}, format="json")

    resp = client.delete(_schedule_url(website, prompt))
    assert resp.status_code == 204
    assert not PromptSchedule.objects.filter(brand_prompt=bp).exists()
    # GET now reports "not scheduled".
    assert _data(client.get(_schedule_url(website, prompt)))["schedule"] is None


@pytest.mark.django_db
def test_post_404_when_prompt_not_saved(auth):
    client, _, website = auth
    prompt = PromptFactory()  # never added to this website as a BrandPrompt
    resp = client.post(
        _schedule_url(website, prompt), {"frequency": "daily"}, format="json",
    )
    assert resp.status_code == 404
    assert not PromptSchedule.objects.exists()


@pytest.mark.django_db
def test_cannot_schedule_on_another_users_website(auth):
    client, _, _ = auth  # authenticated as user A
    other_website = WebsiteFactory()  # owned by a different user
    prompt = PromptFactory()
    _save(other_website, prompt)
    resp = client.post(
        _schedule_url(other_website, prompt), {"frequency": "daily"}, format="json",
    )
    assert resp.status_code in (403, 404)
    assert not PromptSchedule.objects.exists()


# ── Dispatcher ──────────────────────────────────────────────────────────────


def _due_schedule(website, prompt, user, **kwargs):
    bp = _save(website, prompt)
    defaults = dict(
        brand_prompt=bp,
        created_by=user,
        frequency="daily",
        is_enabled=True,
        next_run_at=timezone.now() - timedelta(hours=1),
    )
    defaults.update(kwargs)
    return PromptSchedule.objects.create(**defaults)


@pytest.mark.django_db
def test_due_schedule_enqueues_and_advances():
    website = WebsiteFactory()
    prompt = PromptFactory()
    sched = _due_schedule(website, prompt, website.user)

    with patch("core.ai_tracking.effective_ai_cap", return_value=0.0), patch(
        "apps.prompt_library.tasks.crawl_prompt_for_website"
    ) as crawl:
        dispatch_scheduled_prompt_scans()

    crawl.delay.assert_called_once_with(str(website.id), str(prompt.id))
    sched.refresh_from_db()
    assert sched.last_run_at is not None
    assert sched.next_run_at > timezone.now()
    assert sched.consecutive_failures == 0


@pytest.mark.django_db
def test_not_due_schedule_untouched():
    website = WebsiteFactory()
    prompt = PromptFactory()
    future = timezone.now() + timedelta(days=1)
    sched = _due_schedule(website, prompt, website.user, next_run_at=future)

    with patch("core.ai_tracking.effective_ai_cap", return_value=0.0), patch(
        "apps.prompt_library.tasks.crawl_prompt_for_website"
    ) as crawl:
        dispatch_scheduled_prompt_scans()

    crawl.delay.assert_not_called()
    sched.refresh_from_db()
    assert sched.next_run_at == future


@pytest.mark.django_db
def test_disabled_schedule_ignored():
    website = WebsiteFactory()
    prompt = PromptFactory()
    _due_schedule(website, prompt, website.user, is_enabled=False)

    with patch("core.ai_tracking.effective_ai_cap", return_value=0.0), patch(
        "apps.prompt_library.tasks.crawl_prompt_for_website"
    ) as crawl:
        dispatch_scheduled_prompt_scans()

    crawl.delay.assert_not_called()


@pytest.mark.django_db
def test_over_cap_skips_and_bumps():
    website = WebsiteFactory()
    prompt = PromptFactory()
    sched = _due_schedule(website, prompt, website.user)

    with patch("core.ai_tracking.effective_ai_cap", return_value=10.0), patch(
        "core.ai_tracking.month_to_date_cost", return_value=20.0
    ), patch("apps.prompt_library.tasks.crawl_prompt_for_website") as crawl:
        dispatch_scheduled_prompt_scans()

    crawl.delay.assert_not_called()
    sched.refresh_from_db()
    # Bumped forward so we re-check next cycle rather than tight-loop.
    assert sched.next_run_at > timezone.now()


@pytest.mark.django_db
def test_in_flight_crawl_skips():
    website = WebsiteFactory()
    prompt = PromptFactory()
    sched = _due_schedule(website, prompt, website.user)
    PromptCrawlRun.objects.create(
        website=website, prompt=prompt,
        status=PromptCrawlRun.STATUS_RUNNING,
        started_at=timezone.now(),
    )

    with patch("core.ai_tracking.effective_ai_cap", return_value=0.0), patch(
        "apps.prompt_library.tasks.crawl_prompt_for_website"
    ) as crawl:
        dispatch_scheduled_prompt_scans()

    crawl.delay.assert_not_called()
    sched.refresh_from_db()
    assert sched.next_run_at > timezone.now()  # still bumped


@pytest.mark.django_db
def test_dispatch_failure_autopauses():
    website = WebsiteFactory()
    prompt = PromptFactory()
    sched = _due_schedule(website, prompt, website.user, auto_pause_threshold=1)

    with patch("core.ai_tracking.effective_ai_cap", return_value=0.0), patch(
        "apps.prompt_library.tasks.crawl_prompt_for_website"
    ) as crawl:
        crawl.delay.side_effect = RuntimeError("broker down")
        dispatch_scheduled_prompt_scans()

    sched.refresh_from_db()
    assert sched.consecutive_failures == 1
    assert sched.is_enabled is False  # auto-paused at threshold


@pytest.mark.django_db
def test_record_failure_increments_then_pauses():
    website = WebsiteFactory()
    prompt = PromptFactory()
    sched = _due_schedule(website, prompt, website.user, auto_pause_threshold=3)
    now = timezone.now()
    for _ in range(2):
        _record_prompt_failure(sched, now)
    assert sched.is_enabled is True
    _record_prompt_failure(sched, now)
    assert sched.consecutive_failures == 3
    assert sched.is_enabled is False


def test_frequency_deltas_cover_all_choices():
    assert set(PROMPT_FREQUENCY_DELTAS) == {"daily", "weekly", "monthly"}
    assert PROMPT_FREQUENCY_DELTAS["daily"] == timedelta(days=1)


# ── Detail agg: runs trend + drill-in ───────────────────────────────────────


def _detail_url(website, prompt, **params):
    url = f"{PREFIX}/websites/{website.id}/prompts/{prompt.id}/detail/"
    if params:
        q = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{q}"
    return url


@pytest.mark.django_db
def test_detail_runs_series(auth):
    client, _, website = auth
    prompt = PromptFactory()
    _save(website, prompt)

    audit1 = LLMRankingAuditFactory(website=website)
    audit2 = LLMRankingAuditFactory(website=website)
    LLMRankingResultFactory(
        audit=audit1, source_prompt=prompt, prompt=prompt.text,
        is_mentioned=True, query_succeeded=True,
    )
    LLMRankingResultFactory(
        audit=audit2, source_prompt=prompt, prompt=prompt.text,
        is_mentioned=False, query_succeeded=True,
    )

    resp = client.get(_detail_url(website, prompt))
    assert resp.status_code == 200
    runs = _data(resp)["runs"]
    # One entry per audit that included the prompt.
    assert len(runs) == 2
    run_ids = {r["run_id"] for r in runs}
    assert str(audit1.id) in run_ids and str(audit2.id) in run_ids
    by_id = {r["run_id"]: r for r in runs}
    assert by_id[str(audit1.id)]["visibility_pct"] == 100.0
    assert by_id[str(audit2.id)]["visibility_pct"] == 0.0


@pytest.mark.django_db
def test_detail_domain_brand_sentiment(auth):
    """Per-domain sentiment is the brand's sentiment in the answers that
    cite the domain -- derived from stored responses, no page fetching."""
    from apps.citations.models import Citation

    client, _, website = auth
    prompt = PromptFactory()
    _save(website, prompt)

    audit = LLMRankingAuditFactory(website=website)
    positive = LLMRankingResultFactory(
        audit=audit, source_prompt=prompt, prompt=prompt.text, provider="claude",
        is_mentioned=True, sentiment="positive", query_succeeded=True,
    )
    unmentioned = LLMRankingResultFactory(
        audit=audit, source_prompt=prompt, prompt=prompt.text, provider="gpt4",
        is_mentioned=False, sentiment="not_mentioned", query_succeeded=True,
    )
    for result, domain in ((positive, "goodnews.com"), (unmentioned, "lurker.com")):
        Citation.objects.create(
            result=result, audit=audit,
            url=f"https://{domain}/page", normalized_url=f"https://{domain}/page",
            domain=domain, apex_domain=domain, source_class="editorial",
        )

    body = _data(client.get(_detail_url(website, prompt)))
    by_domain = {d["apex_domain"]: d for d in body["top_domains"]}
    # Cited by an answer that mentioned the brand positively -> 85.
    assert by_domain["goodnews.com"]["brand_sentiment"] == 85.0
    # Cited only by an answer that never mentioned the brand -> null, not 55.
    assert by_domain["lurker.com"]["brand_sentiment"] is None
    # Type rollup carries the same answer-derived sentiment.
    types = {t["key"]: t for t in body["domain_types"]}
    assert types["editorial"]["brand_sentiment"] == 85.0


@pytest.mark.django_db
def test_scan_sources_seeds_from_citations(auth):
    """POST scan-sources creates a SourceScan seeded with one URL per
    cited apex domain, most-cited domains first."""
    from apps.citations.models import Citation, SourceScan

    client, user, website = auth
    prompt = PromptFactory()
    _save(website, prompt)
    audit = LLMRankingAuditFactory(website=website)
    result = LLMRankingResultFactory(
        audit=audit, source_prompt=prompt, prompt=prompt.text, query_succeeded=True,
    )
    # twice.com cited twice (two distinct URLs), once.com once.
    for url, apex in (
        ("https://twice.com/a", "twice.com"),
        ("https://twice.com/b", "twice.com"),
        ("https://once.com/x", "once.com"),
    ):
        Citation.objects.create(
            result=result, audit=audit, url=url, normalized_url=url,
            domain=apex, apex_domain=apex, source_class="editorial",
        )

    with patch("apps.citations.tasks.run_source_scan.delay") as mock_delay:
        resp = client.post(
            f"{PREFIX}/websites/{website.id}/prompts/{prompt.id}/scan-sources/",
        )
    assert resp.status_code == 201
    body = _data(resp)
    assert body["seeded_domains"] == 2
    scan = SourceScan.objects.get(id=body["scan_id"])
    assert scan.source_prompt_id == prompt.id
    assert scan.created_by_id == user.id
    # Most-cited domain's URL first; one URL per apex.
    assert scan.seed_urls == ["https://twice.com/a", "https://once.com/x"]
    mock_delay.assert_called_once_with(str(scan.id))


@pytest.mark.django_db
def test_scan_sources_requires_citations(auth):
    client, _, website = auth
    prompt = PromptFactory()
    _save(website, prompt)
    resp = client.post(
        f"{PREFIX}/websites/{website.id}/prompts/{prompt.id}/scan-sources/",
    )
    assert resp.status_code == 400


@pytest.mark.django_db
def test_detail_page_sentiment_from_seeded_scan(auth):
    """A completed seeded scan surfaces per-domain on-page sentiment in
    the detail payload, mapped from -1..1 onto the 0..100 scale."""
    from apps.citations.models import Citation, SourceScan, SourceScanResult

    client, user, website = auth
    prompt = PromptFactory()
    _save(website, prompt)
    audit = LLMRankingAuditFactory(website=website)
    result = LLMRankingResultFactory(
        audit=audit, source_prompt=prompt, prompt=prompt.text, query_succeeded=True,
    )
    for domain in ("praised.com", "silent.com"):
        Citation.objects.create(
            result=result, audit=audit,
            url=f"https://{domain}/p", normalized_url=f"https://{domain}/p",
            domain=domain, apex_domain=domain, source_class="editorial",
        )

    scan = SourceScan.objects.create(
        website=website, query=prompt.text, source_prompt=prompt,
        seed_urls=["https://praised.com/p", "https://silent.com/p"],
        status="complete", created_by=user, analyzed_count=2, results_count=2,
    )
    SourceScanResult.objects.create(
        scan=scan, rank=1, url="https://praised.com/p", domain="praised.com",
        source_class="editorial", relevant=True, fetch_status="ok",
        brands=[{"name": website.name, "sentiment": 0.7, "weight": 0.9}],
    )
    SourceScanResult.objects.create(
        scan=scan, rank=2, url="https://silent.com/p", domain="silent.com",
        source_class="editorial", relevant=True, fetch_status="ok",
        brands=[{"name": "Someone Else", "sentiment": -0.5, "weight": 0.5}],
    )

    body = _data(client.get(_detail_url(website, prompt)))
    assert body["page_scan"]["status"] == "complete"
    by_domain = {d["apex_domain"]: d for d in body["top_domains"]}
    # 0.7 on -1..1 maps to 85 on 0..100.
    assert by_domain["praised.com"]["page_sentiment"] == 85.0
    assert by_domain["praised.com"]["page_analyzed"] is True
    # Page analyzed but the brand never appeared on it -> null, not neutral.
    assert by_domain["silent.com"]["page_sentiment"] is None
    assert by_domain["silent.com"]["page_analyzed"] is True
    # Overall page tone covers ALL brands on the page, not just ours:
    # praised.com discusses one brand at 0.7 -> 85; silent.com's only
    # brand sits at -0.5 -> 25 (negative-toned content).
    assert by_domain["praised.com"]["page_tone"] == 85.0
    assert by_domain["silent.com"]["page_tone"] == 25.0


@pytest.mark.django_db
def test_detail_never_leaks_raw_provider_errors(auth):
    """Raw provider errors (key validity, console URLs) stay server-side;
    the API returns only a generic category for failed cells and runs."""
    from apps.prompt_library.models import PromptCrawlRun

    client, _, website = auth
    prompt = PromptFactory()
    _save(website, prompt)
    audit = LLMRankingAuditFactory(website=website)
    LLMRankingResultFactory(
        audit=audit, source_prompt=prompt, prompt=prompt.text,
        provider="grok", query_succeeded=False, is_mentioned=False,
        response_text="", sentiment="not_mentioned", mention_rank=None,
        error_message=(
            "Error code: 400 - {'code': 'invalid-argument', 'error': "
            "'Incorrect API key provided. You can obtain an API key from "
            "https://console.x.ai.'}"
        ),
    )
    PromptCrawlRun.objects.create(
        website=website, prompt=prompt, status=PromptCrawlRun.STATUS_FAILED,
        error="grok: Incorrect API key provided https://console.x.ai",
    )

    import json as _json
    body = _data(client.get(_detail_url(website, prompt)))
    blob = _json.dumps(body).lower()
    for secret_hint in ("api key", "console.x.ai", "invalid-argument", "400"):
        assert secret_hint not in blob, f"leaked {secret_hint!r} to the client"
    failed = [c for c in body["recent_chats"] if c["status"] == "failed"]
    assert failed and failed[0]["error"] == "This model did not respond on this scan."
    assert body["latest_scan"]["error"] == (
        "The scan could not complete. The issue has been logged."
    )


@pytest.mark.django_db
def test_detail_run_param_narrows(auth):
    client, _, website = auth
    prompt = PromptFactory()
    _save(website, prompt)

    audit1 = LLMRankingAuditFactory(website=website)
    audit2 = LLMRankingAuditFactory(website=website)
    LLMRankingResultFactory(
        audit=audit1, source_prompt=prompt, prompt=prompt.text, query_succeeded=True,
    )
    LLMRankingResultFactory(
        audit=audit2, source_prompt=prompt, prompt=prompt.text, query_succeeded=True,
    )

    full = _data(client.get(_detail_url(website, prompt)))
    assert full["total_responses"] == 2

    scoped = _data(client.get(_detail_url(website, prompt, run=str(audit1.id))))
    assert scoped["selected_run"] == str(audit1.id)
    assert scoped["total_responses"] == 1  # narrowed to the one run
    # The trend still shows every run, even while drilled in.
    assert len(scoped["runs"]) == 2


# ── Dashboard agg: last_run_at + next_run_at ─────────────────────────────────


def _agg_url(website):
    return f"{PREFIX}/websites/{website.id}/saved-prompts/agg/"


@pytest.mark.django_db
def test_dashboard_next_run_when_scheduled(auth):
    client, user, website = auth
    scheduled = PromptFactory()
    unscheduled = PromptFactory()
    bp = _save(website, scheduled)
    _save(website, unscheduled)
    PromptSchedule.objects.create(
        brand_prompt=bp, created_by=user, frequency="weekly", is_enabled=True,
        next_run_at=timezone.now() + timedelta(days=7),
    )

    rows = {r["id"]: r for r in _data(client.get(_agg_url(website)))["rows"]}
    assert rows[str(scheduled.id)]["next_run_at"] is not None
    assert rows[str(scheduled.id)]["schedule_frequency"] == "weekly"
    assert rows[str(unscheduled.id)]["next_run_at"] is None


@pytest.mark.django_db
def test_dashboard_last_run_from_results(auth):
    client, _, website = auth
    prompt = PromptFactory()
    _save(website, prompt)
    audit = LLMRankingAuditFactory(website=website)
    LLMRankingResultFactory(audit=audit, source_prompt=prompt, prompt=prompt.text)

    rows = {r["id"]: r for r in _data(client.get(_agg_url(website)))["rows"]}
    assert rows[str(prompt.id)]["last_run_at"] is not None


@pytest.mark.django_db
def test_archived_prompt_schedule_is_not_dispatched():
    website = WebsiteFactory()
    prompt = PromptFactory()
    sched = _due_schedule(website, prompt, website.user)
    sched.brand_prompt.is_archived = True
    sched.brand_prompt.save(update_fields=["is_archived"])
    due_at = sched.next_run_at

    with patch("core.ai_tracking.effective_ai_cap", return_value=0.0), patch(
        "apps.prompt_library.tasks.crawl_prompt_for_website"
    ) as crawl, patch("apps.search_gap.tasks.check_google_rank") as google:
        dispatch_scheduled_prompt_scans()

    crawl.delay.assert_not_called()
    google.delay.assert_not_called()
    sched.refresh_from_db()
    # Left due, so unarchiving resumes the schedule on the next cycle.
    assert sched.next_run_at == due_at
    assert sched.last_run_at is None


@pytest.mark.django_db
def test_unarchived_prompt_schedule_resumes():
    website = WebsiteFactory()
    prompt = PromptFactory()
    sched = _due_schedule(website, prompt, website.user)
    bp = sched.brand_prompt
    bp.is_archived = True
    bp.save(update_fields=["is_archived"])
    bp.is_archived = False
    bp.save(update_fields=["is_archived"])

    with patch("core.ai_tracking.effective_ai_cap", return_value=0.0), patch(
        "apps.prompt_library.tasks.crawl_prompt_for_website"
    ) as crawl:
        dispatch_scheduled_prompt_scans()

    crawl.delay.assert_called_once_with(str(website.id), str(prompt.id))
