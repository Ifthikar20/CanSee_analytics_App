"""Domain errors of the agent workspace.

Each maps to the {success: false, error: {code, message, details?}}
envelope through core.interceptors.exception_handler. Codes and statuses
are part of the frontend contract (design/agent_contract.md section 1).
"""
from core.exceptions import CanseeException


class AgentDisabled(CanseeException):
    """AGENT_ENABLED is off."""

    def __init__(self):
        super().__init__(
            "The agent workspace is turned off.",
            code="agent_disabled",
            status_code=503,
        )


class AgentUnavailable(CanseeException):
    """The request reached a process that does not run agents."""

    def __init__(self):
        super().__init__(
            "The agent service is not reachable right now. Please try again shortly.",
            code="agent_unavailable",
            status_code=503,
        )


class AgentNotConfigured(CanseeException):
    """No Anthropic API key, so no run can start."""

    def __init__(self):
        super().__init__(
            "The agent is not configured: ANTHROPIC_API_KEY is not set.",
            code="agent_not_configured",
            status_code=503,
        )


class AllowanceExceeded(CanseeException):
    """The account has used its monthly AI allowance."""

    def __init__(self, *, cap_usd: float, spent_usd: float):
        super().__init__(
            f"You have used your ${cap_usd:.2f} monthly AI allowance.",
            code="allowance_exceeded",
            status_code=403,
            details={"cap_usd": round(cap_usd, 2), "spent_usd": round(spent_usd, 6)},
        )


class AgentBusy(CanseeException):
    """The user already has an active run, or every executor slot is taken."""

    def __init__(self, message: str = "The agent is busy with another run. Try again when it finishes."):
        super().__init__(message, code="agent_busy", status_code=429)


class TooManyStreams(CanseeException):
    """The user already holds the most event streams and polls allowed."""

    def __init__(self):
        super().__init__(
            "Too many open connections to the agent from your account. "
            "Close other tabs and try again.",
            code="too_many_streams",
            status_code=429,
        )


class RunInProgress(CanseeException):
    """This chat already has a queued or running run."""

    def __init__(self, *, run_id=None):
        super().__init__(
            "This chat already has a reply in progress.",
            code="run_in_progress",
            status_code=409,
            details={"run_id": str(run_id)} if run_id else None,
        )


class AlreadySaved(CanseeException):
    """The message was already saved as a report."""

    def __init__(self, *, artifact_id):
        super().__init__(
            "This reply is already saved to Reports.",
            code="already_saved",
            status_code=409,
            details={"artifact_id": str(artifact_id)},
        )


class RevisionConflict(CanseeException):
    """An optimistic-concurrency write was based on a stale revision."""

    def __init__(self, *, revision: int | None = None):
        super().__init__(
            "This was changed since you loaded it. Reload and try again.",
            code="revision_conflict",
            status_code=409,
            details={"revision": revision} if revision is not None else None,
        )
