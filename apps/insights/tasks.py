import logging

from celery import shared_task

logger = logging.getLogger("apps")


@shared_task(name="apps.insights.tasks.run_insight", bind=True, max_retries=0,
             soft_time_limit=900, time_limit=960)
def run_insight(self, run_id: str) -> dict:
    """One insights run (INSIGHTS_RUN_MODE celery or auto), queued on its
    kind's queue (``ai`` or ``integrations``) by runs.dispatch.

    A run that runs out of time ends its unfinished units as failed rather
    than leaving them for the stale sweep.
    """
    from celery.exceptions import SoftTimeLimitExceeded

    from apps.insights.services import runs

    try:
        runs.execute_run(run_id)
    except SoftTimeLimitExceeded:
        logger.warning("insights: run %s timed out", run_id)
        runs.finalize(run_id)
        return {"run_id": run_id, "error": "timeout"}
    return {"run_id": run_id}
