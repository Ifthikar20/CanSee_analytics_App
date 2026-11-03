"""Turn exceptions from a request into a run error the user can read.

Mid-stream failures arrive as APIStatusError with HTTP 200 (an SSE
``error`` event); they are classified by the error type in the body.
Messages never carry raw upstream text.
"""
from __future__ import annotations

from dataclasses import dataclass

import anthropic

from apps.agent.exceptions import AgentNotConfigured

_RETRYABLE_TYPES = {"overloaded_error", "api_error", "timeout_error"}


@dataclass(frozen=True)
class RunError:
    code: str
    retryable: bool
    message: str
    # Worth re-issuing the same request inside the run (with backoff).
    retry_in_run: bool = False

    def as_payload(self) -> dict:
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


OVERLOADED = RunError(
    "upstream_overloaded", True,
    "The AI service is overloaded right now. Please try again in a minute.", True,
)
UPSTREAM = RunError(
    "upstream_error", True, "The AI service had an error. Please try again.", True,
)
UNREACHABLE = RunError(
    "upstream_unreachable", True, "The AI service could not be reached. Please try again.", True,
)
RATE_LIMITED = RunError(
    "rate_limited", True, "The AI service is rate limiting requests. Please try again shortly.",
)
CONTEXT_TOO_LONG = RunError(
    "context_too_long", False, "This chat is too long to continue. Start a new chat.",
)
BAD_REQUEST = RunError(
    "bad_request", False, "The AI service rejected this request.",
)
UPSTREAM_AUTH = RunError(
    "upstream_auth", False, "The agent's API key was rejected. An administrator needs to fix it.",
)
MODEL_NOT_FOUND = RunError(
    "model_not_found", False, "The configured model is not available.",
)
NOT_CONFIGURED = RunError(
    "agent_not_configured", False, "The agent is not configured: ANTHROPIC_API_KEY is not set.",
)
INTERNAL = RunError(
    "internal_error", True, "Something went wrong while writing this reply.",
)


def _error_type(exc: anthropic.APIStatusError) -> str:
    body = exc.body if isinstance(exc.body, dict) else {}
    err = body.get("error") if isinstance(body.get("error"), dict) else {}
    return str(err.get("type") or getattr(exc, "type", "") or "")


def _error_text(exc: anthropic.APIStatusError) -> str:
    body = exc.body if isinstance(exc.body, dict) else {}
    err = body.get("error") if isinstance(body.get("error"), dict) else {}
    return str(err.get("message") or exc.message or "").lower()


def classify(exc: BaseException) -> RunError:
    if isinstance(exc, AgentNotConfigured):
        return NOT_CONFIGURED
    if isinstance(exc, anthropic.APIStatusError):
        kind = _error_type(exc)
        if exc.status_code == 200 or kind in _RETRYABLE_TYPES:
            if kind == "overloaded_error":
                return OVERLOADED
            if kind in _RETRYABLE_TYPES or not kind:
                return UPSTREAM
        if isinstance(exc, anthropic.RateLimitError):
            return RATE_LIMITED
        if exc.status_code == 529:
            return OVERLOADED
        if exc.status_code >= 500:
            return UPSTREAM
        if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
            return UPSTREAM_AUTH
        if isinstance(exc, anthropic.NotFoundError):
            return MODEL_NOT_FOUND
        if exc.status_code == 413 or "prompt is too long" in _error_text(exc):
            return CONTEXT_TOO_LONG
        return BAD_REQUEST
    if isinstance(exc, anthropic.APIConnectionError):
        # APITimeoutError is a subclass.
        return UNREACHABLE
    return INTERNAL
