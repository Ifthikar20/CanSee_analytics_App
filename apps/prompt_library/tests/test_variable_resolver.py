"""Tests for the per-website variable resolver."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from apps.prompt_library.services.variable_resolver import (
    resolve_for_website,
    resolve_variables,
    variables_with_provenance,
)


def _website(*, name="Acme Co", industry="dentistry", locations=None, user_vars=None):
    site = SimpleNamespace(name=name, industry=industry, locations=locations or [])
    if user_vars is not None:
        site.prompt_variables = SimpleNamespace(variables=user_vars)
    else:
        site.prompt_variables = None
    return site


def test_auto_variables_pulled_from_website():
    site = _website(locations=[{"city": "Dallas"}])
    merged = resolve_variables(site)
    assert merged["company_name"] == "Acme Co"
    assert merged["industry"] == "dentistry"
    assert merged["location"] == "Dallas"


def test_user_variables_override_auto():
    site = _website(user_vars={"location": "Austin", "sales_item": "kits"})
    merged = resolve_variables(site)
    assert merged["location"] == "Austin"
    assert merged["sales_item"] == "kits"
    assert merged["company_name"] == "Acme Co"


def test_resolve_for_website_fills_template():
    site = _website(user_vars={"location": "Austin"})
    prompt = MagicMock()
    prompt.template_text = "Best dentist in {{ location }} for {{ company_name }}?"
    prompt.text = ""
    filled, missing = resolve_for_website(site, prompt)
    assert filled == "Best dentist in Austin for Acme Co?"
    assert missing == []


def test_resolve_for_website_falls_back_to_text_when_no_template():
    site = _website()
    prompt = MagicMock()
    prompt.template_text = ""
    prompt.text = "Best CRM?"
    filled, missing = resolve_for_website(site, prompt)
    assert filled == "Best CRM?"
    assert missing == []


def test_provenance_marks_user_overrides():
    site = _website(user_vars={"company_name": "Override Inc", "extra": "x"})
    rows = variables_with_provenance(site)
    by_name = {r["name"]: r for r in rows}
    assert by_name["company_name"]["source"] == "user"
    assert by_name["company_name"]["value"] == "Override Inc"
    assert by_name["extra"]["source"] == "user"
    assert by_name["industry"]["source"] == "auto"
