import logging

from celery import shared_task

logger = logging.getLogger("apps")


# The largest run (10 answers per engine, 5 engines) is 50 answers asked 3 at
# a time: up to about 13 minutes with web search.
@shared_task(name="apps.prompt_box.tasks.run_prompt_box", bind=True, max_retries=0,
             soft_time_limit=1200, time_limit=1260)
def run_prompt_box(self, run_id: str) -> dict:
    """One Ask AI run (PROMPT_BOX_RUN_MODE celery or auto), sent to the ``ai``
    queue by runs.dispatch.

    A run that runs out of time ends its unfinished answers as failed rather
    than leaving them for the stale sweep.
    """
    from celery.exceptions import SoftTimeLimitExceeded

    from apps.prompt_box.services import runs

    try:
        runs.execute_run(run_id)
    except SoftTimeLimitExceeded:
        logger.warning("prompt box: run %s timed out", run_id)
        runs.finalize(run_id)
        return {"run_id": run_id, "error": "timeout"}
    return {"run_id": run_id}
