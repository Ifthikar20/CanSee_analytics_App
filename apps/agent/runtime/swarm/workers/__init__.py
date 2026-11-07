"""The five task kinds of the research agents (design section 8).

A worker takes a TaskJob and returns a WorkerOutput (done, failed or
skipped, with its result line, detail and pane). It reports its steps
through ``job.reporter`` and checks ``job.stopped()`` between them,
raising TaskStopped when the task was stopped, timed out or reached the
research's time limit. Any other exception fails the task with a generic
message (the executor logs it). Workers reuse the research tools'
helpers and never change them.
"""
from __future__ import annotations

from collections.abc import Callable

from apps.agent.constants import WorkerType
from apps.agent.runtime.swarm.context import TaskJob, WorkerOutput
from apps.agent.runtime.swarm.workers import (
    citations,
    content_gap,
    engine_answer,
    page_inspect,
    serp_page,
)

WORKERS: dict[str, Callable[[TaskJob], WorkerOutput]] = {
    WorkerType.SERP_PAGE: serp_page.run,
    WorkerType.ENGINE_ANSWER: engine_answer.run,
    WorkerType.PAGE_INSPECT: page_inspect.run,
    WorkerType.CONTENT_GAP: content_gap.run,
    WorkerType.CITATIONS: citations.run,
}


def first_step(spec: dict, swarm_run) -> str:
    """The step line a task shows when it starts (task.started)."""
    worker = spec.get("worker")
    inputs = spec.get("inputs") or {}
    if worker == WorkerType.SERP_PAGE:
        return f"Fetching Google page {inputs.get('page') or 1}"
    if worker == WorkerType.ENGINE_ANSWER:
        return f"Asking {engine_answer.engine_label(inputs.get('provider'))}"
    if worker == WorkerType.PAGE_INSPECT:
        return "Choosing the page"
    if worker == WorkerType.CONTENT_GAP:
        return content_gap.first_step(spec, swarm_run)
    if worker == WorkerType.CITATIONS:
        return "Collecting cited sources"
    return "Working"
