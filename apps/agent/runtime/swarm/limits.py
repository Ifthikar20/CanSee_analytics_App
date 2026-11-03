"""Process-wide concurrency limits of the research agents (design 7i).

One BoundedSemaphore per resource, created from agent_conf() on first
use: ``task`` (swarm tasks running in this process), ``serp`` (Google
results requests), ``browse`` (page fetches), ``haiku`` (analysis
requests) and ``engine:<provider>`` (one per AI engine). Every swarm of
the process shares them, so two researches running at once still stay
under the providers' limits (ScraperAPI's free plan allows 5 requests at
a time).
"""
from __future__ import annotations

import threading

from apps.agent.conf import agent_conf
from apps.agent.constants import WorkerType

_lock = threading.Lock()
_semaphores: dict[str, threading.BoundedSemaphore] = {}

# The resource each worker's primary action uses. content_gap and
# citations are analyses: their primary resource is the analysis model.
_WORKER_RESOURCE = {
    WorkerType.SERP_PAGE: "serp",
    WorkerType.PAGE_INSPECT: "browse",
    WorkerType.CONTENT_GAP: "haiku",
    WorkerType.CITATIONS: "haiku",
}


def primary_resource(spec: dict) -> str | None:
    """The resource a task holds from its start until its primary call
    returns: serp, browse, haiku or engine:<provider>."""
    worker = spec.get("worker")
    if worker == WorkerType.ENGINE_ANSWER:
        provider = (spec.get("inputs") or {}).get("provider")
        return f"engine:{provider}" if provider else None
    return _WORKER_RESOURCE.get(worker)


def holds_haiku(worker: str) -> bool:
    """Whether a task of ``worker`` already holds an analysis slot."""
    return _WORKER_RESOURCE.get(worker) == "haiku"


def _size(name: str) -> int:
    conf = agent_conf()
    if name == "task":
        return conf.swarm_process_workers
    if name == "serp":
        return conf.swarm_serp_concurrency
    if name == "browse":
        return conf.swarm_browse_concurrency
    if name == "haiku":
        return conf.swarm_haiku_concurrency
    if name.startswith("engine:"):
        return conf.swarm_engine_concurrency
    raise KeyError(f"Unknown swarm resource: {name}")


def _semaphore(name: str) -> threading.BoundedSemaphore:
    semaphore = _semaphores.get(name)
    if semaphore is None:
        with _lock:
            semaphore = _semaphores.get(name)
            if semaphore is None:
                semaphore = threading.BoundedSemaphore(max(1, _size(name)))
                _semaphores[name] = semaphore
    return semaphore


def try_acquire(name: str) -> bool:
    """Take one slot of ``name`` when one is free, without waiting."""
    return _semaphore(name).acquire(blocking=False)


def acquire(name: str, timeout: float) -> bool:
    """Take one slot of ``name``, waiting at most ``timeout`` seconds."""
    if timeout <= 0:
        return try_acquire(name)
    return _semaphore(name).acquire(timeout=timeout)


def release(name: str) -> None:
    _semaphore(name).release()


def reset_limits() -> None:
    """Forget every semaphore; the next use builds them from the settings
    of now (tests)."""
    with _lock:
        _semaphores.clear()
