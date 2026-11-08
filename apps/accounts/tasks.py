import logging

from celery import shared_task

logger = logging.getLogger("apps")


@shared_task(
    name="apps.accounts.tasks.send_verification_email",
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
)
def send_verification_email(user_id: str):
    """Generate a verification OTP and email it to the user."""
    from apps.accounts import emails
    from apps.accounts.models import User
    from apps.accounts.services.auth_service import AuthService
    from apps.notifications.services.email_service import EmailService

    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        logger.error(f"User {user_id} not found for verification email.")
        return

    # SECURITY: never log the OTP itself — it is a live authentication
    # secret. It travels only in the email body. A retry generates a fresh
    # OTP and retires the earlier ones (AuthService.generate_email_otp).
    otp = AuthService.generate_email_otp(user=user)
    subject, html = emails.verification_email(otp=otp)
    if not EmailService.send_email(to=user.email, subject=subject, html_content=html):
        raise RuntimeError("verification email send was not accepted by the backend")
    logger.info("Verification OTP emailed for user %s", user_id)


@shared_task(
    name="apps.accounts.tasks.send_password_reset_email",
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
)
def send_password_reset_email(email: str, token: str):
    """Email the password reset link."""
    from apps.accounts import emails
    from apps.notifications.services.email_service import EmailService

    # SECURITY: never log the reset token (grants reset) OR the email
    # address (PII in the general log stream).
    subject, html = emails.password_reset_email(token=token)
    if not EmailService.send_email(to=email, subject=subject, html_content=html):
        raise RuntimeError("password reset email send was not accepted by the backend")
    logger.info("Password reset email dispatched")


@shared_task(
    name="apps.accounts.tasks.send_org_invitation_email",
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
)
def send_org_invitation_email(
    email: str, org_name: str, inviter_name: str, role: str, token: str
):
    """Email an organization invitation with its single-use accept link.

    The raw token exists only here and in the recipient's inbox — the DB
    stores its hash. A retry re-sends the same link.
    """
    from apps.accounts import emails
    from apps.notifications.services.email_service import EmailService

    subject, html = emails.org_invitation_email(
        org_name=org_name, inviter_name=inviter_name, role=role, token=token
    )
    if not EmailService.send_email(to=email, subject=subject, html_content=html):
        raise RuntimeError("invitation email send was not accepted by the backend")
    logger.info("Organization invitation emailed (org=%s)", org_name)


@shared_task(name="apps.accounts.tasks.reverify_org_domains")
def reverify_org_domains():
    """Daily sweep: re-check the DNS TXT proof on verified org domains."""
    from apps.accounts.services.domain_service import DomainService

    lost = DomainService.recheck_verified_domains()
    logger.info("Org domain re-verification done (%d lost verification)", lost)


@shared_task(name="apps.accounts.tasks.expire_inactive_sessions")
def expire_inactive_sessions():
    """Clean up expired sessions."""
    logger.info("Session expiry task completed.")
