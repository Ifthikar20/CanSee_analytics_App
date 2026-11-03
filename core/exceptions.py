class CanseeException(Exception):
    """Base exception for all domain errors.

    ``details`` is an optional, CLIENT-VISIBLE dict the exception handler
    passes through as ``error.details`` — only ever put data in it that was
    written for the end user (an org name, a login method list), never raw
    internals.
    """

    def __init__(
        self,
        message: str,
        code: str = "error",
        status_code: int = 400,
        details: dict | None = None,
    ):
        self.message = message
        self.code = code
        self.status_code = status_code
        self.details = details or {}
        super().__init__(message)


class PlanLimitExceeded(CanseeException):
    def __init__(self, message="You've reached your plan limit."):
        super().__init__(message, code="plan_limit_exceeded", status_code=403)


class ResourceNotFound(CanseeException):
    def __init__(self, message="Resource not found."):
        super().__init__(message, code="not_found", status_code=404)


class PixelNotVerified(CanseeException):
    def __init__(self):
        super().__init__(
            "Tracking pixel is not yet verified.", code="pixel_not_verified"
        )


class AuditInProgress(CanseeException):
    def __init__(self):
        super().__init__("A prompt run is already in progress.", code="audit_in_progress")


class AIGenerationFailed(CanseeException):
    def __init__(self):
        super().__init__(
            "AI generation failed. Please try again.",
            code="ai_failed",
            status_code=503,
        )


class CompetitorLimitReached(CanseeException):
    def __init__(self):
        super().__init__(
            "Competitor tracking limit reached for your plan.",
            code="competitor_limit",
        )


class DuplicateWebsite(CanseeException):
    def __init__(self):
        super().__init__(
            "You already have a project tracking this URL.",
            code="duplicate_website",
        )


class InvalidWebsiteURL(CanseeException):
    def __init__(self):
        super().__init__(
            "The provided URL is not a valid, reachable website.",
            code="invalid_url",
        )


class PermissionDenied(CanseeException):
    def __init__(self, message="You do not have permission to perform this action."):
        super().__init__(message, code="permission_denied", status_code=403)


class WebsiteNotFound(ResourceNotFound):
    def __init__(self):
        super().__init__("Website not found.")


class LeadNotFound(ResourceNotFound):
    def __init__(self):
        super().__init__("Lead not found.")


class DomainOwnershipRequired(CanseeException):
    def __init__(self):
        super().__init__(
            "You can only run services on websites you own. Please verify ownership first.",
            code="domain_ownership_required",
            status_code=403,
        )


class RateLimited(CanseeException):
    def __init__(self):
        super().__init__(
            "You're making requests too quickly. Please wait a moment and try again.",
            code="rate_limited",
            status_code=429,
        )


class SsoRequired(CanseeException):
    """The account's organization mandates single sign-on.

    Raised on every password-credential surface (login, password reset
    request/redemption, password change) for members of an org with
    ``require_sso``. ``details`` carries what the SPA needs to route the
    user to the right button: {org_name, domain, methods}.
    """

    def __init__(self, details: dict | None = None):
        super().__init__(
            "Your organization requires single sign-on. Use your company "
            "identity provider to sign in.",
            code="sso_required",
            status_code=403,
            details=details,
        )


class LoginRefused(CanseeException):
    """A password sign-in that cannot go ahead, with a message safe to show.

    Wrong credentials answer 400, not 401: the SPA treats any 401 as an
    expired session and would try to refresh it.
    """

    def __init__(self, message: str, *, code: str, status_code: int = 400):
        super().__init__(message, code=code, status_code=status_code)


class SuspiciousInput(CanseeException):
    def __init__(self):
        super().__init__(
            "Your request was blocked for security reasons.",
            code="suspicious_input",
            status_code=400,
        )

