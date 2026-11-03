"""
Generic circuit breaker for outbound calls to third-party APIs.

Use one instance per upstream dependency (Polar, Search Console, GA4, etc.).
Trips open after `failure_threshold` failures inside `window_seconds`, blocks
calls for `recovery_timeout` seconds, then admits a single probe call.

State is held in Redis so all Celery workers and web processes share it. If
Redis is unreachable the breaker fails open (allows the call) so a cache
outage cannot itself take the platform down.

Usage:
    breaker = CircuitBreaker(name="polar", failure_threshold=5, recovery_timeout=60)

    if not breaker.allow():
        raise CircuitBreakerOpen("polar")

    try:
        result = call_polar()
    except TransientPolarError:
        breaker.record_failure()
        raise
    else:
        breaker.record_success()

Or via the context-manager helper:

    with breaker.guard():
        call_polar()
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from enum import StrEnum

logger = logging.getLogger("core.resilience")


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerOpen(Exception):
    """Raised when a call is short-circuited because the breaker is open."""

    def __init__(self, name: str):
        super().__init__(f"Circuit breaker [{name}] is open")
        self.name = name


class CircuitBreaker:
    """Redis-backed circuit breaker. Safe to instantiate at module level."""

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 5,
        recovery_timeout: int = 60,
        window_seconds: int = 60,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.window_seconds = window_seconds

    # ── Redis keys ──
    @property
    def _failures_key(self) -> str:
        return f"cb:{self.name}:failures"

    @property
    def _opened_at_key(self) -> str:
        return f"cb:{self.name}:opened_at"

    def _redis(self):
        try:
            from django.core.cache import cache
            return cache
        except Exception:
            return None

    # ── State ──
    def state(self) -> CircuitState:
        cache = self._redis()
        if cache is None:
            return CircuitState.CLOSED
        try:
            opened_at = cache.get(self._opened_at_key)
        except Exception as exc:
            logger.warning("CircuitBreaker[%s] cache read failed: %s", self.name, exc)
            return CircuitState.CLOSED

        if not opened_at:
            return CircuitState.CLOSED
        if time.time() - float(opened_at) >= self.recovery_timeout:
            return CircuitState.HALF_OPEN
        return CircuitState.OPEN

    def allow(self) -> bool:
        return self.state() in (CircuitState.CLOSED, CircuitState.HALF_OPEN)

    def record_success(self) -> None:
        cache = self._redis()
        if cache is None:
            return
        try:
            cache.delete(self._failures_key)
            cache.delete(self._opened_at_key)
        except Exception as exc:
            logger.warning("CircuitBreaker[%s] cache clear failed: %s", self.name, exc)

    def record_failure(self) -> None:
        cache = self._redis()
        if cache is None:
            return
        try:
            failures = cache.get(self._failures_key) or 0
            failures = int(failures) + 1
            cache.set(self._failures_key, failures, timeout=self.window_seconds)
            if failures >= self.failure_threshold:
                cache.set(self._opened_at_key, time.time(), timeout=self.recovery_timeout)
                logger.warning(
                    "CircuitBreaker[%s] OPEN after %d failures in %ds",
                    self.name,
                    failures,
                    self.window_seconds,
                )
        except Exception as exc:
            logger.warning("CircuitBreaker[%s] cache write failed: %s", self.name, exc)

    @contextmanager
    def guard(self):
        if not self.allow():
            raise CircuitBreakerOpen(self.name)
        try:
            yield
        except Exception:
            self.record_failure()
            raise
        else:
            self.record_success()

    def status(self) -> dict:
        return {
            "name": self.name,
            "state": self.state().value,
            "failure_threshold": self.failure_threshold,
            "recovery_timeout": self.recovery_timeout,
            "window_seconds": self.window_seconds,
        }
