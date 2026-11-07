"""Tests for the {{ variable }} template parser."""
from __future__ import annotations

import pytest

from apps.prompt_library.services.template_parser import (
    extract_variables,
    fill_template,
    has_unfilled_variables,
)


def test_extract_variables_returns_ordered_unique():
    text = "Hi {{ name }}, you are {{ role }} at {{ name }} co."
    assert extract_variables(text) == ["name", "role"]


def test_extract_variables_empty_text():
    assert extract_variables("") == []
    assert extract_variables(None) == []  # type: ignore[arg-type]


def test_extract_variables_ignores_non_identifiers():
    text = "{{ 1bad }} {{ ok }} {{ also_ok }}"
    assert extract_variables(text) == ["ok", "also_ok"]


def test_fill_template_replaces_known_keys():
    out, missing = fill_template(
        "Hello {{ name }} from {{ city }}.",
        {"name": "Sam", "city": "Dallas"},
    )
    assert out == "Hello Sam from Dallas."
    assert missing == []


def test_fill_template_leaves_missing_in_place():
    out, missing = fill_template(
        "Hi {{ name }} of {{ company_name }}.",
        {"name": "Sam"},
    )
    assert "{{ company_name }}" in out
    assert missing == ["company_name"]


def test_fill_template_treats_empty_string_as_missing():
    _, missing = fill_template("{{ a }}", {"a": ""})
    assert missing == ["a"]


def test_fill_template_strict_raises():
    with pytest.raises(ValueError):
        fill_template("Hi {{ x }}", {}, strict=True)


def test_has_unfilled_variables():
    assert has_unfilled_variables("Hi {{ x }}") is True
    assert has_unfilled_variables("Hi there") is False
    assert has_unfilled_variables("") is False
