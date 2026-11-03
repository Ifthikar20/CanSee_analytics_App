"""The ad campaign sheet: response schema, validation and limits.

Structured outputs enforce the shape but drop length constraints, so the
Google Ads limits are checked here. Text over a limit is kept as written
and reported in ``warnings`` (never truncated silently). So are landing
page URLs (final_url, sitelinks) that are not http(s), not on the
brand's website, or not taken from the data the writer was given: an ad
must not point at a page nobody knows exists.
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError

HEADLINE_MAX = 30
HEADLINES_MIN, HEADLINES_MAX = 3, 15
DESCRIPTION_MAX = 90
DESCRIPTIONS_MIN, DESCRIPTIONS_MAX = 2, 4
PATH_MAX = 15
SITELINK_TEXT_MAX = 25
SITELINK_DESCRIPTION_MAX = 35

_NULLABLE_STRING = {"anyOf": [{"type": "string"}, {"type": "null"}]}


def _obj(properties: dict) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


# Every field is required (nullable where optional): no length limits,
# which structured outputs would drop anyway.
RESPONSE_SCHEMA = _obj({
    "campaign": _obj({
        "name": {"type": "string"},
        "objective": {"type": "string"},
        "platform": {"type": "string", "enum": ["google_search"]},
        "daily_budget": {"anyOf": [{"type": "number"}, {"type": "null"}]},
        "bid_strategy": _NULLABLE_STRING,
        "final_url": {"type": "string"},
    }),
    "ad_groups": {
        "type": "array",
        "items": _obj({
            "name": {"type": "string"},
            "keywords": {
                "type": "array",
                "items": _obj({
                    "text": {"type": "string"},
                    "match": {"type": "string", "enum": ["exact", "phrase", "broad"]},
                }),
            },
            "negatives": {"type": "array", "items": {"type": "string"}},
            "ads": {
                "type": "array",
                "items": _obj({
                    "headlines": {"type": "array", "items": {"type": "string"}},
                    "descriptions": {"type": "array", "items": {"type": "string"}},
                    "path1": _NULLABLE_STRING,
                    "path2": _NULLABLE_STRING,
                }),
            },
        }),
    },
    "sitelinks": {
        "type": "array",
        "items": _obj({
            "text": {"type": "string"},
            "url": {"type": "string"},
            "description1": _NULLABLE_STRING,
            "description2": _NULLABLE_STRING,
        }),
    },
})


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Campaign(_Model):
    name: str
    objective: str
    platform: Literal["google_search"] = "google_search"
    daily_budget: float | None = None
    bid_strategy: str | None = None
    final_url: str


class Keyword(_Model):
    text: str
    match: Literal["exact", "phrase", "broad"]


class Ad(_Model):
    headlines: list[str]
    descriptions: list[str]
    path1: str | None = None
    path2: str | None = None


class AdGroup(_Model):
    name: str
    keywords: list[Keyword] = Field(default_factory=list)
    negatives: list[str] = Field(default_factory=list)
    ads: list[Ad] = Field(default_factory=list)


class Sitelink(_Model):
    text: str
    url: str
    description1: str | None = None
    description2: str | None = None


class AdCampaign(_Model):
    campaign: Campaign
    ad_groups: list[AdGroup] = Field(default_factory=list)
    sitelinks: list[Sitelink] = Field(default_factory=list)


class SheetInvalid(Exception):
    """The output is not a campaign (not JSON, or the wrong shape)."""


def _too_long(warnings, path: str, value: str | None, limit: int, what: str) -> None:
    if value and len(value) > limit:
        warnings.append({"path": path,
                         "message": f"{what} has {len(value)} characters; the limit is {limit}."})


def limit_warnings(campaign: AdCampaign) -> list[dict]:
    warnings: list[dict] = []
    if not campaign.ad_groups:
        warnings.append({"path": "ad_groups", "message": "The campaign has no ad groups."})
    for g, group in enumerate(campaign.ad_groups):
        base = f"ad_groups[{g}]"
        if not group.keywords:
            warnings.append({"path": f"{base}.keywords", "message": "The ad group has no keywords."})
        if not group.ads:
            warnings.append({"path": f"{base}.ads", "message": "The ad group has no ads."})
        for a, ad in enumerate(group.ads):
            ad_path = f"{base}.ads[{a}]"
            if not HEADLINES_MIN <= len(ad.headlines) <= HEADLINES_MAX:
                warnings.append({
                    "path": f"{ad_path}.headlines",
                    "message": f"The ad has {len(ad.headlines)} headlines; "
                               f"Google allows {HEADLINES_MIN} to {HEADLINES_MAX}.",
                })
            for h, text in enumerate(ad.headlines):
                _too_long(warnings, f"{ad_path}.headlines[{h}]", text, HEADLINE_MAX, "Headline")
            if not DESCRIPTIONS_MIN <= len(ad.descriptions) <= DESCRIPTIONS_MAX:
                warnings.append({
                    "path": f"{ad_path}.descriptions",
                    "message": f"The ad has {len(ad.descriptions)} descriptions; "
                               f"Google allows {DESCRIPTIONS_MIN} to {DESCRIPTIONS_MAX}.",
                })
            for d, text in enumerate(ad.descriptions):
                _too_long(warnings, f"{ad_path}.descriptions[{d}]", text, DESCRIPTION_MAX,
                          "Description")
            _too_long(warnings, f"{ad_path}.path1", ad.path1, PATH_MAX, "Path 1")
            _too_long(warnings, f"{ad_path}.path2", ad.path2, PATH_MAX, "Path 2")
    for s, link in enumerate(campaign.sitelinks):
        path = f"sitelinks[{s}]"
        _too_long(warnings, f"{path}.text", link.text, SITELINK_TEXT_MAX, "Sitelink text")
        _too_long(warnings, f"{path}.description1", link.description1, SITELINK_DESCRIPTION_MAX,
                  "Sitelink description")
        _too_long(warnings, f"{path}.description2", link.description2, SITELINK_DESCRIPTION_MAX,
                  "Sitelink description")
    return warnings


def _link_key(url: str) -> str:
    """``url`` as compared with the known ones: host without www., path
    without a trailing slash, the query; scheme and fragment ignored."""
    try:
        parts = urlsplit((url or "").strip())
        host = (parts.hostname or "").lower()
    except ValueError:
        return ""
    host = host.removeprefix("www.")
    query = f"?{parts.query}" if parts.query else ""
    return f"{host}{parts.path.rstrip('/')}{query}"


def _url_problem(url: str, apex: str, known: set[str]) -> str | None:
    from apps.search_gap.services.google_rank import is_same_site

    try:
        parts = urlsplit((url or "").strip())
        scheme, host = parts.scheme.lower(), parts.hostname or ""
    except ValueError:
        scheme, host = "", ""
    if scheme not in ("http", "https") or not host:
        return "This is not an http(s) link to a page."
    if apex and not is_same_site(host, apex):
        return "This URL is not on the brand's website."
    if _link_key(url) not in known:
        return ("This URL does not appear in the brand data or the brief; check that the page "
                "exists before uploading.")
    return None


def url_warnings(campaign: AdCampaign, *, site_url: str, known_urls: Iterable[str]) -> list[dict]:
    """Warnings for the campaign's final_url and sitelink URLs."""
    from apps.search_gap.services.google_rank import site_apex

    apex = site_apex(site_url) if site_url else ""
    known = {_link_key(u) for u in known_urls}
    targets = [("campaign.final_url", campaign.campaign.final_url)]
    targets += [(f"sitelinks[{i}].url", link.url) for i, link in enumerate(campaign.sitelinks)]
    warnings = []
    for path, url in targets:
        problem = _url_problem(url, apex, known)
        if problem:
            warnings.append({"path": path, "message": problem})
    return warnings


def validate_sheet(raw: str, *, site_url: str | None = None,
                   known_urls: Iterable[str] | None = None) -> tuple[dict, list[dict]]:
    """(sheet content with ``warnings``, warnings) from the model's JSON text.

    With ``known_urls`` (the URLs of the writer's data) the landing page
    URLs are checked too; they are reported, never rewritten.
    """
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise SheetInvalid("The campaign is not valid JSON.") from exc
    try:
        campaign = AdCampaign.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc") or ())
        raise SheetInvalid(f"The campaign does not match the sheet shape ({where}).") from exc
    warnings = limit_warnings(campaign)
    if known_urls is not None:
        warnings += url_warnings(campaign, site_url=site_url or "", known_urls=known_urls)
    content = campaign.model_dump(mode="json")
    content["warnings"] = warnings
    return content, warnings


def summarize_sheet(content: dict) -> str:
    groups = content.get("ad_groups") or []
    ads = sum(len(g.get("ads") or []) for g in groups)
    keywords = sum(len(g.get("keywords") or []) for g in groups)
    name = (content.get("campaign") or {}).get("name") or "Campaign"
    return (f"{name}: {len(groups)} ad groups, {ads} ads, {keywords} keywords, "
            f"{len(content.get('sitelinks') or [])} sitelinks")[:500]
