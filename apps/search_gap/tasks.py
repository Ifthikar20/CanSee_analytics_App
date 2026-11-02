import logging

from celery import shared_task

logger = logging.getLogger("apps")


@shared_task(name="apps.search_gap.tasks.check_google_rank", bind=True, max_retries=0)
def check_google_rank(self, brand_prompt_id: str, force: bool = False) -> dict:
    """Google rank check for one tracked prompt.

    Queued by the prompt page's "Check now" and by the per-prompt schedule
    dispatcher, so Google is rechecked on the same cadence as the AI
    engines. A missing key or a spent daily allowance is a quiet no-op.
    """
    from apps.prompt_library.models import BrandPrompt
    from apps.search_gap.services import google_rank

    try:
        brand_prompt = BrandPrompt.objects.select_related("website", "prompt").get(id=brand_prompt_id)
    except BrandPrompt.DoesNotExist:
        return {"error": "brand_prompt_not_found"}

    try:
        check = google_rank.check(brand_prompt, force=force)
    except google_rank.NotConfigured:
        return {"error": "serpapi_not_configured"}
    except google_rank.DailyLimitReached:
        logger.warning(
            "search_gap: daily Google check allowance reached for website %s",
            brand_prompt.website_id,
        )
        return {"error": "daily_limit"}
    return {"check_id": str(check.id), "position": check.position, "error": check.error}
