"""Tests for the Brand Research lanes layered on the Source Intelligence scan.

Covers the pieces added when Search Insights became Brand Research: per-lane
stage reporting, the Google SerpAPI discovery lane, the Reddit community lane,
and the AI-engine probe.
"""

from unittest.mock import MagicMock, patch

import pytest

from apps.citations.models import SourceScan, SourceScanStatus
from apps.citations.services import source_scan, web_search
from apps.websites.tests.factories import WebsiteFactory


def _resp(payload, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=payload)
    return resp


SERP_PAYLOAD = {
    "results": [
        {"url": "https://www.reddit.com/r/Dallas/comments/abc/best_bagels/",
         "title": "Where are the best bagels at??", "snippet": "..."},
        {"url": "https://dallasobserver.com/best-bagels", "title": "Ranked", "snippet": "..."},
    ]
}

ANALYSIS = {
    "relevant_to_query": True,
    "brands": [
        {"name": "Starship Bagel", "mentions": 3, "sentiment": 0.9, "weight": 0.9,
         "quotes": ["Starship without question."], "issues": []},
    ],
    "target_brand_present": False,
}

READ_OK = {"status": "ok", "kind": "page", "text": "Starship is great.",
           "word_count": 3, "detail": ""}


TERMINAL = {source_scan.COMPLETE, source_scan.SKIPPED, source_scan.FAILED}


# -- stage reporting ----------------------------------------------------------


@pytest.mark.django_db
def test_stages_all_terminal_on_success(settings):
    """Every lane must settle, or the UI spins on it forever."""
    settings.PERPLEXITY_API_KEY = "pplx-test"
    website = WebsiteFactory(name="Brooklyn Bagel Co")
    scan = SourceScan.objects.create(website=website, query="best bagels in dallas")

    with patch.object(web_search.requests, "post", return_value=_resp(SERP_PAYLOAD)), \
         patch.object(source_scan.content_reader, "read_url", return_value=READ_OK), \
         patch.object(source_scan.source_sentiment, "analyze_content", return_value=ANALYSIS):
        source_scan.run_scan(scan)

    scan.refresh_from_db()
    assert scan.status == SourceScanStatus.COMPLETE
    assert set(scan.stages) == set(source_scan.STAGES)
    for name, lane in scan.stages.items():
        assert lane["status"] in TERMINAL, f"lane {name} left at {lane['status']}"
    assert scan.stages["web"]["status"] == source_scan.COMPLETE
    assert scan.stages["web"]["count"] == 2
    assert scan.stages["analysis"]["count"] == 2


@pytest.mark.django_db
def test_stages_all_terminal_on_failure(settings):
    """A scan that dies before any lane runs still settles the whole strip."""
    settings.PERPLEXITY_API_KEY = ""
    website = WebsiteFactory()
    scan = SourceScan.objects.create(website=website, query="q")

    source_scan.run_scan(scan)

    scan.refresh_from_db()
    assert scan.status == SourceScanStatus.FAILED
    assert scan.stages, "a failed scan must still report its lanes"
    for name, lane in scan.stages.items():
        assert lane["status"] in TERMINAL, f"lane {name} left at {lane['status']}"
    assert scan.stages["web"]["status"] == source_scan.FAILED


@pytest.mark.django_db
def test_finalize_stages_downgrades_running_to_failed():
    """A lane interrupted mid-flight reads as failed; one never entered reads
    as skipped. The distinction is what the UI needs to tell 'nothing to do'
    from 'something broke'."""
    website = WebsiteFactory()
    scan = SourceScan.objects.create(website=website, query="q")
    source_scan._init_stages(scan)
    source_scan._set_stage(scan, "web", source_scan.RUNNING)
    source_scan._set_stage(scan, "analysis", source_scan.COMPLETE, count=4)

    source_scan._finalize_stages(scan, reason="worker died")

    scan.refresh_from_db()
    assert scan.stages["web"]["status"] == source_scan.FAILED
    assert scan.stages["web"]["detail"] == "worker died"
    assert scan.stages["community"]["status"] == source_scan.SKIPPED
    # Already-terminal lanes are left exactly as they were.
    assert scan.stages["analysis"]["status"] == source_scan.COMPLETE
    assert scan.stages["analysis"]["count"] == 4


@pytest.mark.django_db
def test_set_stage_never_raises():
    """Progress reporting must not be able to kill the scan it reports on."""
    website = WebsiteFactory()
    scan = SourceScan.objects.create(website=website, query="q")
    source_scan._init_stages(scan)

    with patch.object(type(scan), "save", side_effect=RuntimeError("db gone")):
        source_scan._set_stage(scan, "web", source_scan.RUNNING)
        source_scan._finalize_stages(scan)
