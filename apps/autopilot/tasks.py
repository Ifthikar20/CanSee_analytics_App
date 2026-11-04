"""Autopilot's Celery tasks (backend 4e).

``dispatch_autopilot`` runs on beat every 5 minutes (``default`` queue).
Each stage of a run is a task of its own, and each hands the run to the
next stage when it ends (``pipeline.kick``): the plan, the engines and the
analysis on ``ai``, Google and the text on ``integrations``, finalize on
``default`` (config/celery.py routes them). The engines and Google steps
stop claiming prompts after AUTOPILOT_STEP_SECONDS and re-queue themselves;
their soft time limit leaves 360 s for the prompts in flight.
"""

import logging

from celery import shared_task
from django.conf import settings

logger = logging.getLogger("apps")

_STEP_SECONDS = int(getattr(settings, "AUTOPILOT_STEP_SECONDS", 1200))


@shared_task(name="apps.autopilot.tasks.dispatch_autopilot", max_retries=0,
             soft_time_limit=200, time_limit=230)
def dispatch_autopilot() -> dict:
    """One dispatcher tick: due runs, finished analyses, silent stages, texts."""
    from apps.autopilot.services import pipeline

    return pipeline.dispatch_tick(mode="celery")


@shared_task(name="apps.autopilot.tasks.start_run", max_retries=0,
             soft_time_limit=240, time_limit=300)
def start_run(run_id: str) -> dict:
    from apps.autopilot.services import pipeline

    if pipeline.start_run(run_id):
        pipeline.kick(run_id, mode="celery")
    return {"run_id": run_id}


def _step(step, run_id: str) -> dict:
    from celery.exceptions import SoftTimeLimitExceeded

    from apps.autopilot.services import pipeline

    try:
        outcome = step(run_id)
    except SoftTimeLimitExceeded:
        # The dispatcher resumes the step once it stops reporting.
        logger.warning("autopilot: run %s ran out of time in %s", run_id, step.__name__)
        return {"run_id": run_id, "error": "timeout"}
    if outcome != pipeline.IDLE:
        pipeline.kick(run_id, mode="celery")
    return {"run_id": run_id, "outcome": outcome}


@shared_task(name="apps.autopilot.tasks.run_engines", max_retries=0,
             soft_time_limit=_STEP_SECONDS + 360, time_limit=_STEP_SECONDS + 420)
def run_engines(run_id: str) -> dict:
    from apps.autopilot.services import pipeline

    return _step(pipeline.run_engines, run_id)


@shared_task(name="apps.autopilot.tasks.run_google", max_retries=0,
             soft_time_limit=_STEP_SECONDS + 360, time_limit=_STEP_SECONDS + 420)
def run_google(run_id: str) -> dict:
    from apps.autopilot.services import pipeline

    return _step(pipeline.run_google, run_id)


@shared_task(name="apps.autopilot.tasks.finalize_run", max_retries=0,
             soft_time_limit=240, time_limit=300)
def finalize_run(run_id: str) -> dict:
    from apps.autopilot.services import pipeline

    run = pipeline.finalize_run(run_id)
    pipeline.deliver_due(run_id, mode="celery")
    return {"run_id": run_id, "status": run.status if run is not None else None}


@shared_task(name="apps.autopilot.tasks.deliver_text", max_retries=0,
             soft_time_limit=60, time_limit=90)
def deliver_text(run_id: str) -> dict:
    from apps.autopilot.services import text

    return {"run_id": run_id, "outcome": text.deliver_text(run_id)}
