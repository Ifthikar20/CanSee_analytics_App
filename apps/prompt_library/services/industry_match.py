"""Map a website to a prompt-library Industry."""
from __future__ import annotations

from django.utils.text import slugify

UNCATEGORIZED_SLUG = "uncategorized"


def matching_industry(website):
    """The active Industry matching the website's industry text, or None."""
    from apps.prompt_library.models import Industry

    raw = (getattr(website, "industry", "") or "").strip()
    if not raw:
        return None
    return (
        Industry.objects.filter(is_active=True, slug__iexact=slugify(raw)).first()
        or Industry.objects.filter(is_active=True, name__iexact=raw).first()
    )


def industry_for_website(website):
    """The matching Industry, else the inactive "Uncategorized" bucket.

    Every Prompt row needs an Industry. Filing a website's prompts under an
    unrelated industry would mislabel them and offer them as suggestions
    to other websites in that industry; the inactive bucket is never
    listed, mined or suggested.
    """
    from apps.prompt_library.models import Industry

    match = matching_industry(website)
    if match is not None:
        return match
    industry, _ = Industry.objects.get_or_create(
        slug=UNCATEGORIZED_SLUG,
        defaults={"name": "Uncategorized", "is_active": False},
    )
    return industry
