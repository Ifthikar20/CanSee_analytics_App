import logging
import secrets
import string
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import authenticate
from django.core.cache import cache
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import (
    EmailVerificationOTP,
    LoginAttempt,
    PasswordResetToken,
    User,
)
from core.exceptions import CanseeException, LoginRefused
from core.logging.audit_logger import audit_log

security_logger = logging.getLogger("security")

# TokenService.issue_session's refusals of a password sign-in, by code.
_SESSION_REFUSALS = {
    "Please verify your email before logging in.": "email_not_verified",
    "This account has been deactivated.": "account_deactivated",
}


#: How long an email verification code stays valid.
OTP_TTL_MINUTES = 15
#: Wrong codes allowed per account in OTP_FAILURE_WINDOW_SECONDS. At the
#: limit the live code is retired and verification is refused until the
#: window ends. Sending a new code does not reset the count, so resending
#: cannot buy more guesses.
OTP_MAX_FAILURES = 10
OTP_FAILURE_WINDOW_SECONDS = 60 * 60
#: A new code can be emailed to an account at most once per this period.
OTP_RESEND_COOLDOWN_SECONDS = 60


def _otp_failures_key(user) -> str:
    return f"email_otp_failures:{user.pk}"


def otp_resend_allowed(user) -> bool:
    """True at most once per OTP_RESEND_COOLDOWN_SECONDS for ``user``."""
    return cache.add(f"email_otp_resend:{user.pk}", 1, timeout=OTP_RESEND_COOLDOWN_SECONDS)


def _otp_locked() -> CanseeException:
    return CanseeException(
        "Too many incorrect codes. Wait up to an hour, then request a new code.",
        code="otp_attempts_exceeded",
        status_code=429,
    )


class AuthService:
    """
    Handles all authentication business logic.
    Views are thin — they call this service and return responses.
    """

    @staticmethod
    def _enforce_password_policy(password: str, user=None) -> None:
        """Run the configured AUTH_PASSWORD_VALIDATORS (common-password,
        numeric-only, user-attribute-similarity, length). set_password /
        create_user do NOT validate, so every write path calls this or
        the validators are silently bypassed."""
        from django.contrib.auth.password_validation import validate_password
        from django.core.exceptions import ValidationError

        try:
            validate_password(password, user)
        except ValidationError as exc:
            raise ValueError(" ".join(exc.messages)) from exc

    @staticmethod
    def register(
        *, email: str, password: str, full_name: str, company_name: str = ""
    ) -> User:
        """Create a new user account with email verification pending."""
        if User.objects.filter(email__iexact=email).exists():
            raise ValueError("An account with this email already exists.")

        AuthService._enforce_password_policy(password)

        user = User.objects.create_user(
            email=email,
            password=password,
            full_name=full_name,
            company_name=company_name,
            is_email_verified=False,
        )

        audit_log("user.registered", user=user, action="create", resource_type="user", resource_id=str(user.id), metadata={"method": "email"})
        return user

    @staticmethod
    def login(
        *, email: str, password: str, ip_address: str, user_agent: str, request=None
    ) -> dict:
        """Authenticate user and return JWT token pair."""
        # SSO enforcement runs BEFORE the password is checked: an enforced
        # account must get the same answer whether or not the password is
        # right, and a correct password must never mint a session.
        email = (email or "").strip().lower()
        AuthService._require_sso_gate(email=email)

        user = authenticate(request=request, email=email, password=password)

        if user is None and getattr(request, "axes_locked_out", False):
            # django-axes refused the attempt before the password was
            # checked. Say so: "Incorrect email or password" would send the
            # user round in circles for the whole cool-off.
            security_logger.warning(
                "Login attempt on locked-out email",
                extra={"email": email, "ip": ip_address},
            )
            minutes = int(settings.AXES_COOLOFF_TIME.total_seconds() // 60) if settings.AXES_COOLOFF_TIME else 30
            raise LoginRefused(
                f"Too many failed sign-in attempts. Try again in {minutes} minutes or reset your password.",
                code="account_locked",
                status_code=429,
            )

        if user is None:
            LoginAttempt.objects.create(
                email=email,
                ip_address=ip_address,
                user_agent=user_agent,
                success=False,
            )
            security_logger.warning(
                "Failed login attempt",
                extra={"email": email, "ip": ip_address},
            )
            # Never says which of the two was wrong.
            raise LoginRefused("Incorrect email or password.", code="invalid_credentials")

        # Verified/active checks, the success LoginAttempt row, claims and
        # audit all live in the shared factory so every login method behaves
        # identically.
        from apps.accounts.services.token_service import TokenService

        try:
            return TokenService.issue_session(
                user, method="pwd", ip_address=ip_address, user_agent=user_agent
            )
        except ValueError as exc:
            # issue_session's own refusals are written for users; anything
            # else stays a generic error.
            code = _SESSION_REFUSALS.get(str(exc))
            if code is None:
                raise
            raise LoginRefused(str(exc), code=code, status_code=403) from exc

    @staticmethod
    def _require_sso_gate(*, email: str) -> None:
        """Raise SsoRequired when ``email`` belongs to an SSO-enforced org.

        Applies to every password-credential surface: login, password
        reset (request and redemption), and password change. Membership is
        what matters, not the email's domain — a member invited under a
        different domain is enforced too.
        """
        from core.exceptions import SsoRequired
        from core.permissions.org import org_features_enabled

        # Master switch: no business flows, no SSO enforcement.
        if not org_features_enabled():
            return

        user = User.objects.filter(email__iexact=email).first()
        if user is None:
            return
        membership = (
            user.org_memberships.select_related("organization")
            .order_by("created_at")
            .first()
        )
        if membership and membership.organization.require_sso:
            org = membership.organization
            raise SsoRequired(
                details={
                    "org_name": org.name,
                    "domain": email.rsplit("@", 1)[-1].lower(),
                    "methods": AuthService.sso_methods_for(org),
                }
            )

    @staticmethod
    def sso_methods_for(org) -> list[str]:
        """The IdP buttons the SPA should offer for this org's SSO gate.

        Google is always available; Microsoft appears once the org has
        registered an Entra tenant id on any of its domains; SAML appears
        once ops record the org's bridge connection id (SSOReady/WorkOS).
        """
        methods = ["google"]
        if org.domains.exclude(entra_tenant_id="").exists():
            methods.append("microsoft")
        # Gated on sso_connection_id alone — deliberately NOT also on the
        # bridge API keys, so a key misconfig surfaces as a visible button
        # answering 503 from /auth/saml/start/, never as a silently
        # missing option.
        if org.sso_connection_id:
            methods.append("saml")
        return methods

    @staticmethod
    def logout(*, refresh_token: str, user: User) -> None:
        """Blacklist the refresh token to invalidate the session."""
        try:
            token = RefreshToken(refresh_token)
            token.blacklist()
            audit_log("user.logout", user=user, action="logout", resource_type="user", resource_id=str(user.id))
        except Exception:
            pass

    @staticmethod
    def refresh_token(*, refresh_token: str) -> dict:
        """Rotate the refresh token and issue a new access token."""
        from apps.accounts.services.token_service import TokenService

        return TokenService.rotate_session(refresh_token=refresh_token)

    @staticmethod
    def generate_email_otp(*, user: User) -> str:
        """Generate a 6-digit OTP for email verification.

        Only the newest code is valid: every earlier unused code is retired
        first, so resending cannot stack up many live codes to guess at.
        """
        otp = "".join(secrets.choice(string.digits) for _ in range(6))
        expires_at = timezone.now() + timedelta(minutes=OTP_TTL_MINUTES)
        EmailVerificationOTP.objects.filter(user=user, used=False).update(used=True)
        EmailVerificationOTP.objects.create(
            user=user, otp=otp, expires_at=expires_at
        )
        return otp

    @staticmethod
    def verify_email_otp(*, email: str, otp: str) -> User:
        """Verify the OTP and mark the user's email as verified."""
        try:
            user = User.objects.get(email__iexact=email)
        except User.DoesNotExist:
            raise ValueError("Invalid OTP.") from None

        failures_key = _otp_failures_key(user)
        if (cache.get(failures_key) or 0) >= OTP_MAX_FAILURES:
            raise _otp_locked()

        verification = (
            EmailVerificationOTP.objects.filter(
                user=user,
                otp=otp,
                used=False,
                expires_at__gt=timezone.now(),
            )
            .order_by("-created_at")
            .first()
        )

        if not verification:
            # Count wrong guesses per account, whatever IP they come from.
            # At the limit the live code is retired and the account is
            # locked until the window ends; then only a new code works.
            cache.add(failures_key, 0, timeout=OTP_FAILURE_WINDOW_SECONDS)
            try:
                failures = cache.incr(failures_key)
            except ValueError:  # expired between add and incr
                cache.set(failures_key, 1, timeout=OTP_FAILURE_WINDOW_SECONDS)
                failures = 1
            if failures >= OTP_MAX_FAILURES:
                EmailVerificationOTP.objects.filter(user=user, used=False).update(used=True)
                raise _otp_locked()
            raise ValueError("Invalid or expired OTP.")

        cache.delete(failures_key)
        verification.used = True
        verification.save(update_fields=["used"])

        user.is_email_verified = True
        user.save(update_fields=["is_email_verified"])

        audit_log("user.email_verified", user=user, action="update", resource_type="user", resource_id=str(user.id))
        return user

    @staticmethod
    def generate_password_reset_token(*, email: str) -> str:
        """Generate a secure password reset token."""
        try:
            user = User.objects.get(email__iexact=email, is_active=True)
        except User.DoesNotExist:
            # Don't reveal whether the email exists
            return ""

        # SSO-enforced members can't hold a usable password, so a reset
        # link must never be minted for them — otherwise "reset password"
        # is a one-click bypass of enforcement. Silent no-op (not an
        # error) to preserve the endpoint's non-enumeration contract.
        try:
            AuthService._require_sso_gate(email=email)
        except Exception:
            audit_log(
                "user.password_reset_blocked_sso",
                user=user,
                action="update",
                resource_type="user",
                resource_id=str(user.id),
            )
            return ""

        token = secrets.token_urlsafe(48)
        expires_at = timezone.now() + timedelta(hours=1)
        PasswordResetToken.objects.create(user=user, token=token, expires_at=expires_at)
        return token

    @staticmethod
    def reset_password(*, token: str, new_password: str) -> User:
        """Reset password using a valid reset token."""
        try:
            reset_token = PasswordResetToken.objects.get(
                token=token, used=False, expires_at__gt=timezone.now()
            )
        except PasswordResetToken.DoesNotExist:
            raise ValueError("Invalid or expired reset token.") from None

        user = reset_token.user
        # A token minted before the org flipped enforcement on must not
        # redeem after it.
        AuthService._require_sso_gate(email=user.email)
        AuthService._enforce_password_policy(new_password, user)
        user.set_password(new_password)
        user.save(update_fields=["password"])

        reset_token.used = True
        reset_token.save(update_fields=["used"])

        audit_log("user.password_reset", user=user, action="update", resource_type="user", resource_id=str(user.id))
        return user

    @staticmethod
    def change_password(*, user: User, old_password: str, new_password: str) -> None:
        """Change password for an authenticated user."""
        AuthService._require_sso_gate(email=user.email)
        if not user.check_password(old_password):
            raise ValueError("Current password is incorrect.")
        AuthService._enforce_password_policy(new_password, user)
        user.set_password(new_password)
        user.save(update_fields=["password"])
        audit_log("user.password_changed", user=user, action="update", resource_type="user", resource_id=str(user.id))
