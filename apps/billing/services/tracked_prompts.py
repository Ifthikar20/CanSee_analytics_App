"""The plan's cap on tracked prompts (wave 4 contract 0c and 2).

An account tracks the active (unarchived) BrandPrompt rows of every project
it can act on: ``WebsiteService.accessible_qs`` (its own projects, or its
organization's), the tenant the project cap counts. The account of a project
is its owner (``website.user``): the cap at every place a prompt becomes
tracked and Autopilot's run list both resolve the plan of ``website.user``,
whoever makes the request, so the two never disagree.

Nothing is archived when an account is over its cap (a downgrade, prompts
tracked before the cap existed): Autopilot runs the account's oldest
``limit`` prompts and counts the rest as over the limit.

No lock is taken: two racing requests can pass the cap by a few rows, and
Autopilot never runs more than the limit.
"""

from __future__ import annotations

from django.db.models import Q

from core.exceptions import CanseeException
from core.utils.constants import PLAN_NAMES, Plan

# What the 403 asks the account to do next, by plan (Business is unlimited).
NEXT_FREE = "Archive some or choose the 100 or 200 prompt plan."
NEXT_UPGRADE = f"Archive some or move to {PLAN_NAMES[Plan.PROMPTS_200]}."
NEXT_ARCHIVE = "Archive some to add more."


def limit_for(user) -> int:
    """Prompts the user's plan tracks; -1 is unlimited."""
    from apps.billing.services.plan_limits import get_limits

    raw = get_limits(user).get("tracked_prompts", -1)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return -1
    return raw


def _tracked(user):
    """Active brand prompts on every project the user can act on."""
    from apps.prompt_library.models import BrandPrompt
    from apps.websites.services.website_service import WebsiteService

    return BrandPrompt.objects.filter(
        is_archived=False, website__in=WebsiteService.accessible_qs(user),
    )


def used_by(user) -> int:
    """Prompts the user's account tracks now."""
    if user is None or not getattr(user, "pk", None):
        return 0
    return _tracked(user).count()


def capacity(user) -> dict:
    """``{"limit": int, "used": int}`` for the session and billing payloads."""
    return {"limit": limit_for(user), "used": used_by(user)}


def adding_count(website, *, prompt_ids=(), text_hashes=(), restore: bool = False) -> int:
    """How many of the request's distinct prompts would become tracked on
    ``website``.

    Each prompt is named once, by its id or by the text hash the entry point
    uses to find its ``Prompt``. A prompt that already has an active
    BrandPrompt on the project adds nothing. An archived row adds one only
    with ``restore`` (the entry points that un-archive it); elsewhere
    ``get_or_create`` leaves it archived, so it adds nothing.
    """
    from apps.prompt_library.models import BrandPrompt

    ids = {str(value) for value in prompt_ids if value}
    hashes = {str(value) for value in text_hashes if value}
    if not ids and not hashes:
        return 0
    rows = BrandPrompt.objects.filter(website=website).filter(
        Q(prompt_id__in=ids) | Q(prompt__text_hash__in=hashes),
    )
    if restore:
        rows = rows.filter(is_archived=False)
    have_ids, have_hashes = set(), set()
    for prompt_id, digest in rows.values_list("prompt_id", "prompt__text_hash"):
        have_ids.add(str(prompt_id))
        have_hashes.add(digest)
    return len(ids - have_ids) + len(hashes - have_hashes)


def _next_step(user) -> str:
    from apps.billing.services.plan_limits import _resolve_plan_key

    plan = _resolve_plan_key(user)
    if plan in (Plan.PROMPTS_200, Plan.BUSINESS):
        return NEXT_ARCHIVE
    if plan == Plan.FREE:
        return NEXT_FREE
    # Pro, 100 prompts, and an unknown plan (which gets Pro's limits).
    return NEXT_UPGRADE


def limit_message(user, limit: int, used: int) -> str:
    """The 403 wording, the same at every entry point."""
    verb = "is" if used == 1 else "are"
    return (
        f"Your plan tracks up to {limit} prompts and {used} {verb} tracked. "
        f"{_next_step(user)}"
    )


def check_capacity(website, adding: int) -> None:
    """Raise 403 ``tracked_prompt_limit`` when tracking ``adding`` more
    prompts on ``website`` would pass its owner's cap. Callers check before
    they create anything, so a refused request adds nothing."""
    adding = max(0, int(adding or 0))
    if not adding:
        return
    owner = website.user
    limit = limit_for(owner)
    if limit < 0:
        return
    used = used_by(owner)
    if used + adding <= limit:
        return
    raise CanseeException(
        limit_message(owner, limit, used),
        code="tracked_prompt_limit",
        status_code=403,
        details={"limit": limit, "used": used, "adding": adding},
    )


def runnable_brand_prompts(website) -> tuple[list, int]:
    """``(brand prompts, over_limit)``: the project's active prompts that fall
    within its owner's cap, oldest first (with ``prompt`` selected), and how
    many of its active prompts fall outside it.

    The cap keeps the account's oldest ``limit`` prompts across all its
    projects, ordered by ``(created_at, id)``.
    """
    from apps.prompt_library.models import BrandPrompt

    mine = list(
        BrandPrompt.objects.filter(website=website, is_archived=False)
        .select_related("prompt")
        .order_by("created_at", "id")
    )
    owner = website.user
    limit = limit_for(owner)
    if limit < 0:
        return mine, 0
    within = set(
        _tracked(owner).order_by("created_at", "id").values_list("id", flat=True)[:limit]
    )
    runnable = [bp for bp in mine if bp.id in within]
    return runnable, len(mine) - len(runnable)
