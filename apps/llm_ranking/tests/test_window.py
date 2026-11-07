"""Tests for the analytics window resolver."""
from datetime import datetime, timedelta

import pytest
from django.utils import timezone

from apps.llm_ranking.services._window import (
    EPOCH,
    PRESET_DAYS,
    parse_prompt_filter,
    resolve_window,
)


def _within(actual, expected, *, tolerance_seconds=5):
    return abs((actual - expected).total_seconds()) <= tolerance_seconds


class TestResolveWindow:
    def test_default_falls_back_to_30_days(self):
        now = timezone.now()
        start, end = resolve_window(None, None, None)
        assert _within(end, now)
        assert _within(start, now - timedelta(days=30))

    @pytest.mark.parametrize("preset,days", PRESET_DAYS.items())
    def test_presets_use_named_days(self, preset, days):
        start, end = resolve_window(preset, None, None)
        assert _within(start, end - timedelta(days=days))

    def test_overall_uses_epoch(self):
        start, end = resolve_window("overall", None, None)
        assert start == EPOCH
        assert end >= EPOCH

    def test_custom_uses_supplied_dates(self):
        start, end = resolve_window("custom", "2024-01-01", "2024-02-15")
        assert start == timezone.make_aware(datetime(2024, 1, 1))
        assert end.year == 2024 and end.month == 2 and end.day == 15

    def test_custom_falls_back_when_invalid(self):
        now = timezone.now()
        start, _ = resolve_window("custom", "not-a-date", None)
        assert _within(start, now - timedelta(days=30))


class TestParsePromptFilter:
    def test_returns_none_for_none(self):
        assert parse_prompt_filter(None) is None

    def test_splits_newline_string(self):
        assert parse_prompt_filter("a\nb\n") == ["a", "b"]

    def test_list_returns_trimmed_strings(self):
        assert parse_prompt_filter([" a ", "b", ""]) == ["a", "b"]

    def test_empty_list_returns_none(self):
        assert parse_prompt_filter([]) is None
