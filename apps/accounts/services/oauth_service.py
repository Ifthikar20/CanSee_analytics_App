"""Google and Microsoft Entra sign-in: server-side code exchange +
verified id_token identity.

Identity rules (the EASIE pattern):

- The signed **id_token** is the only source of identity claims. The old
  implementation read the userinfo endpoint and ignored the token, which
  left ``sub`` (the stable account key), ``email_verified``, and ``hd``
  (Workspace hosted domain) unavailable.
- Users are keyed on ``sub`` via SocialIdentity — an email change at
  Google must not orphan or duplicate an account. Email matching is used
  once, to link an existing Cansee account, and only because Google
  asserts ``email_verified``.
- ``hd`` is trusted only FROM the verified token, and even then it only
  proves the account belongs to that Workspace — auto-join additionally
  requires the org to have DNS-verified the domain (a defunct company's
  domain can be re-registered and its Workspace resurrected by a
  stranger; a live TXT record proves present-day control).
- JIT provisioning grants the org's ``default_role`` (member), never
  ownership.
"""
import logging
import time

import requests
from django.conf import settings
from django.utils import timezone

from apps.accounts.models import OrgDomain, SocialIdentity, User
from core.exceptions import CanseeException, PermissionDenied
from core.logging.audit_logger import audit_log
from core.permissions.org import org_features_enabled

logger = logging.getLogger("apps")

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Microsoft Entra ID (work/school accounts only — the 'organizations'
# authority refuses personal-account sign-in at the authorize step, and
# _verify_entra_id_token re-rejects the consumer tenant server-side).
ENTRA_TOKEN_URL = (
    "https://login.microsoftonline.com/organizations/oauth2/v2.0/token"
)
ENTRA_JWKS_URL = "https://login.microsoftonline.com/common/discovery/v2.0/keys"
# The fixed tenant id Microsoft uses for consumer (personal) accounts.
ENTRA_CONSUMER_TENANT = "9188040d-6c67-4c5b-b112-36a304b66dad"
_ENTRA_JWKS_TTL_SECONDS = 24 * 60 * 60


class DomainUnclaimed(CanseeException):
    """A Workspace account signed in but its domain has no Cansee org."""

    def __init__(self, domain: str):
        super().__init__(
            f"@{domain} isn't set up on Cansee yet. Contact us to bring "
            "your team on board.",
            code="domain_unclaimed",
            status_code=403,
            details={"domain": domain},
        )


def _verify_id_token(raw_id_token: str) -> dict:
    """Validate signature/audience/issuer and return the claims."""
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token as google_id_token

    claims = google_id_token.verify_oauth2_token(
        raw_id_token, google_requests.Request(), settings.GOOGLE_OAUTH_CLIENT_ID
    )
    if claims.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        raise ValueError("Unexpected id_token issuer.")
    return claims


# Entra signing keys rotate rarely; cache the JWKS for a day and refetch
# once on a kid miss (rotation between refreshes) before failing.
_entra_jwks_cache: dict = {"jwks": None, "fetched_at": 0.0}


def _entra_jwks(*, force: bool = False) -> dict:
    """The cached Microsoft Entra JWKS document."""
    now = time.monotonic()
    expired = now - _entra_jwks_cache["fetched_at"] > _ENTRA_JWKS_TTL_SECONDS
    if force or _entra_jwks_cache["jwks"] is None or expired:
        response = requests.get(ENTRA_JWKS_URL, timeout=10)
        response.raise_for_status()
        _entra_jwks_cache["jwks"] = response.json()
        _entra_jwks_cache["fetched_at"] = now
    return _entra_jwks_cache["jwks"]


def _verify_entra_id_token(raw_id_token: str) -> dict:
    """Validate signature/audience/issuer/tenant and return the claims.

    Personal Microsoft accounts (the fixed consumer tenant) are rejected
    here: every downstream trust decision keys on ``tid`` being a real
    organization's tenant.
    """
    from authlib.jose import jwt as jose_jwt
    from authlib.jose.errors import JoseError

    try:
        try:
            claims = jose_jwt.decode(raw_id_token, key=_entra_jwks())
        except (KeyError, ValueError):
            # kid not in the cached set — the signing keys may have
            # rotated since the last fetch. Refetch once, then let
            # failures surface.
            claims = jose_jwt.decode(raw_id_token, key=_entra_jwks(force=True))
        claims.validate()
    except JoseError as exc:
        # Forged/expired/garbled tokens are a client error (400), not an
        # application crash.
        raise ValueError("Invalid Microsoft id_token.") from exc

    if claims.get("aud") != settings.MICROSOFT_OAUTH_CLIENT_ID:
        raise ValueError("Unexpected id_token audience.")
    tid = (claims.get("tid") or "").strip().lower()
    if not tid:
        raise ValueError("id_token is missing the tid (tenant) claim.")
    if claims.get("iss") != f"https://login.microsoftonline.com/{tid}/v2.0":
        raise ValueError("Unexpected id_token issuer.")
    if tid == ENTRA_CONSUMER_TENANT:
        raise PermissionDenied(
            "Personal Microsoft accounts can't sign in here — use your "
            "work account."
        )

    verified = dict(claims)
    verified["tid"] = tid
    return verified


class OAuthService:
    @staticmethod
    def google_authenticate(
        *, code: str, redirect_uri: str, invite_token: str = ""
    ) -> dict:
        """Authenticate (and where authorized, provision) via Google.

        Returns {user, org, joined_org, is_new_user}. Never creates an
        account except through one of the two explicitly authorized lanes:
        a valid invitation token, or domain-JIT against a DNS-verified,
        auto-join org domain. Everything else keeps the login-only 403 —
        SIGNUPS_ENABLED stays meaningful.
        """
        token_response = requests.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
                "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=10,
        )
        token_response.raise_for_status()
        tokens = token_response.json()

        raw_id_token = tokens.get("id_token", "")
        if not raw_id_token:
            raise ValueError("Google did not return an id_token.")
        claims = _verify_id_token(raw_id_token)

        if not claims.get("email_verified"):
            raise PermissionDenied(
                "This Google account's email address is not verified."
            )

        sub = claims["sub"]
        email = (claims.get("email") or "").strip().lower()
        hd = (claims.get("hd") or "").strip().lower()
        full_name = claims.get("name") or email.split("@", 1)[0]
        if not email:
            raise ValueError("Google account did not provide an email address.")

        # 1. Stable-key lookup.
        identity = (
            SocialIdentity.objects.select_related("user")
            .filter(provider="google", subject=sub)
            .first()
        )
        user = identity.user if identity else None

        # 2. One-time link to an existing account by verified email.
        if user is None:
            user = User.objects.filter(email__iexact=email).first()
            if user is not None:
                identity = SocialIdentity.objects.create(
                    user=user,
                    provider="google",
                    subject=sub,
                    tenant=hd,
                    email_at_link=email,
                )

        joined_org = False
        is_new_user = False
        org = None

        # Master business-features switch: while off, the invitation and
        # domain-JIT lanes never provision — an unknown Google account
        # falls straight to the login-only refusal, exactly as pre-B2B.
        if not org_features_enabled():
            invite_token = ""
            hd = ""

        # 3. Invitation lane (works for new AND existing accounts): the
        #    emailed token authorizes both the signup and the membership.
        if invite_token:
            from apps.accounts.services.org_service import OrgService

            invitation = OrgService.invitation_by_token(invite_token)
            if invitation.email != email:
                raise CanseeException(
                    "This invitation was sent to a different email address.",
                    code="invitation_email_mismatch",
                    status_code=403,
                )
            # Atomic: a seat-cap refusal inside add_member must roll back
            # the account and identity created for this invitation.
            from django.db import transaction

            with transaction.atomic():
                if user is None:
                    user = User.objects.create_user(
                        email=email,
                        password=None,
                        full_name=full_name,
                        company_name=invitation.organization.name,
                        is_email_verified=True,
                    )
                    is_new_user = True
                    identity = SocialIdentity.objects.create(
                        user=user,
                        provider="google",
                        subject=sub,
                        tenant=hd,
                        email_at_link=email,
                    )
                OrgService.add_member(
                    organization=invitation.organization,
                    user=user,
                    role=invitation.role,
                    invited_by=invitation.invited_by,
                    joined_via="invite",
                )
                invitation.accepted_at = timezone.now()
                invitation.save(update_fields=["accepted_at"])
            org = invitation.organization
            joined_org = True

        # 4. Domain-JIT lane: a Workspace account whose domain an org has
        #    DNS-verified with auto-join on.
        elif user is None and hd:
            record = (
                OrgDomain.objects.select_related("organization")
                .filter(domain=hd, verified_at__isnull=False, auto_join=True)
                .first()
            )
            if record is None:
                audit_log(
                    "user.login_rejected",
                    user=None,
                    action="login",
                    resource_type="user",
                    resource_id=email,
                    success=False,
                    metadata={"method": "google_oauth", "reason": "domain_unclaimed", "hd": hd},
                )
                raise DomainUnclaimed(hd)

            from django.db import transaction

            from apps.accounts.services.org_service import OrgService

            # Atomic: a seat-cap rejection inside add_member must roll the
            # just-created user back, or it strands a passwordless orphan
            # account that also blocks this email from future invites.
            with transaction.atomic():
                user = User.objects.create_user(
                    email=email,
                    password=None,
                    full_name=full_name,
                    company_name=record.organization.name,
                    is_email_verified=True,
                )
                is_new_user = True
                identity = SocialIdentity.objects.create(
                    user=user,
                    provider="google",
                    subject=sub,
                    tenant=hd,
                    email_at_link=email,
                )
                OrgService.add_member(
                    organization=record.organization,
                    user=user,
                    role=record.organization.default_role,
                    joined_via="domain_jit",
                )
            org = record.organization
            joined_org = True
            audit_log(
                "org.member_jit_joined",
                user=user,
                action="create",
                resource_type="organization",
                resource_id=str(record.organization_id),
                metadata={"hd": hd},
            )

        # 5. Consumer/unknown account, no authorization to provision.
        if user is None:
            audit_log(
                "user.login_rejected",
                user=None,
                action="login",
                resource_type="user",
                resource_id=email,
                success=False,
                metadata={"method": "google_oauth", "reason": "no_account"},
            )
            raise PermissionDenied(
                "No Cansee account exists for this Google email. "
                "Contact your administrator for access."
            )

        if identity is not None:
            identity.last_login_at = timezone.now()
            update_fields = ["last_login_at"]
            if hd and identity.tenant != hd:
                identity.tenant = hd
                update_fields.append("tenant")
            identity.save(update_fields=update_fields)

        if org is None and org_features_enabled():
            membership = (
                user.org_memberships.select_related("organization")
                .order_by("created_at")
                .first()
            )
            org = membership.organization if membership else None

        audit_log(
            "user.login",
            user=user,
            action="login",
            resource_type="user",
            resource_id=str(user.id),
            metadata={"method": "google_oauth"},
        )

        return {
            "user": user,
            "org": org,
            "joined_org": joined_org,
            "is_new_user": is_new_user,
        }

    @staticmethod
    def entra_authenticate(
        *, code: str, redirect_uri: str, invite_token: str = ""
    ) -> dict:
        """Authenticate (and where authorized, provision) via Microsoft Entra.

        Returns {user, org, joined_org, is_new_user} — the Google shape.

        Entra's ``email`` claim is NOT trustworthy the way Google's
        ``email_verified`` one is: any tenant admin can put an arbitrary
        address on their own users (the nOAuth attack), and ``xms_edov``
        is too unreliable to rescue it. So email is only ever believed
        when something else vouches for it:

        - the emailed invitation token (the token is the credential;
          the email match is a consistency check), or
        - an OrgDomain the org has DNS-verified AND registered with this
          exact ``tid`` — the org has declared the tenant authoritative
          for its domain, for both account-linking and domain-JIT.

        Everything else keys purely on (oid, tid) or is rejected.
        """
        token_response = requests.post(
            ENTRA_TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.MICROSOFT_OAUTH_CLIENT_ID,
                "client_secret": settings.MICROSOFT_OAUTH_CLIENT_SECRET,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=10,
        )
        token_response.raise_for_status()
        tokens = token_response.json()

        raw_id_token = tokens.get("id_token", "")
        if not raw_id_token:
            raise ValueError("Microsoft did not return an id_token.")
        claims = _verify_entra_id_token(raw_id_token)

        oid = claims["oid"]
        tid = claims["tid"]
        email = (
            claims.get("email") or claims.get("preferred_username") or ""
        ).strip().lower()
        full_name = claims.get("name") or email.split("@", 1)[0]
        email_domain = email.rsplit("@", 1)[-1] if "@" in email else ""

        # 1. Stable-key lookup: (oid, tid) survives any email change.
        identity = (
            SocialIdentity.objects.select_related("user")
            .filter(provider="entra", subject=oid, tenant=tid)
            .first()
        )
        user = identity.user if identity else None

        joined_org = False
        is_new_user = False
        org = None

        # 2. Invitation lane (works for new AND existing accounts): the
        #    emailed token authorizes signup, linking, and membership.
        if invite_token:
            from django.db import transaction

            from apps.accounts.services.org_service import OrgService

            invitation = OrgService.invitation_by_token(invite_token)
            if invitation.email != email:
                raise CanseeException(
                    "This invitation was sent to a different email address.",
                    code="invitation_email_mismatch",
                    status_code=403,
                )
            with transaction.atomic():
                if user is None:
                    user = User.objects.filter(email__iexact=email).first()
                    if user is None:
                        user = User.objects.create_user(
                            email=email,
                            password=None,
                            full_name=full_name,
                            company_name=invitation.organization.name,
                            is_email_verified=True,
                        )
                        is_new_user = True
                    identity = SocialIdentity.objects.create(
                        user=user,
                        provider="entra",
                        subject=oid,
                        tenant=tid,
                        email_at_link=email,
                    )
                OrgService.add_member(
                    organization=invitation.organization,
                    user=user,
                    role=invitation.role,
                    invited_by=invitation.invited_by,
                    joined_via="invite",
                )
                invitation.accepted_at = timezone.now()
                invitation.save(update_fields=["accepted_at"])
            org = invitation.organization
            joined_org = True

        # 3+4. Tenant-authoritative domain lanes. Both linking an existing
        #      account by email and domain-JIT require the org to have
        #      DNS-verified the domain AND registered this tenant id —
        #      without the tid match the email claim proves nothing.
        elif user is None and email_domain:
            record = (
                OrgDomain.objects.select_related("organization")
                .filter(
                    domain=email_domain,
                    verified_at__isnull=False,
                    entra_tenant_id=tid,
                )
                .first()
            )

            # 3. Link an existing Cansee account by email.
            if record is not None:
                existing = User.objects.filter(email__iexact=email).first()
                if existing is not None:
                    user = existing
                    identity = SocialIdentity.objects.create(
                        user=user,
                        provider="entra",
                        subject=oid,
                        tenant=tid,
                        email_at_link=email,
                    )

            # 4. Domain-JIT provisioning.
            if user is None and record is not None and record.auto_join:
                from django.db import transaction

                from apps.accounts.services.org_service import OrgService

                # Atomic: a seat-cap rejection inside add_member must roll
                # the just-created user back, or it strands a passwordless
                # orphan account (see the Google lane).
                with transaction.atomic():
                    user = User.objects.create_user(
                        email=email,
                        password=None,
                        full_name=full_name,
                        company_name=record.organization.name,
                        is_email_verified=True,
                    )
                    is_new_user = True
                    identity = SocialIdentity.objects.create(
                        user=user,
                        provider="entra",
                        subject=oid,
                        tenant=tid,
                        email_at_link=email,
                    )
                    OrgService.add_member(
                        organization=record.organization,
                        user=user,
                        role=record.organization.default_role,
                        joined_via="domain_jit",
                    )
                org = record.organization
                joined_org = True
                audit_log(
                    "org.member_jit_joined",
                    user=user,
                    action="create",
                    resource_type="organization",
                    resource_id=str(record.organization_id),
                    metadata={"tid": tid, "domain": email_domain},
                )

            # 5. Work domain with no qualifying registration.
            if user is None:
                audit_log(
                    "user.login_rejected",
                    user=None,
                    action="login",
                    resource_type="user",
                    resource_id=email,
                    success=False,
                    metadata={
                        "method": "entra_oauth",
                        "reason": "domain_unclaimed",
                        "tid": tid,
                    },
                )
                raise DomainUnclaimed(email_domain)

        # 6. Unknown account, no authorization to provision.
        if user is None:
            audit_log(
                "user.login_rejected",
                user=None,
                action="login",
                resource_type="user",
                resource_id=email,
                success=False,
                metadata={"method": "entra_oauth", "reason": "no_account"},
            )
            raise PermissionDenied(
                "No Cansee account exists for this Microsoft account. "
                "Contact your administrator for access."
            )

        if identity is not None:
            # tenant is part of the lookup key, so unlike Google's ``hd``
            # it can never drift — only the login timestamp moves.
            identity.last_login_at = timezone.now()
            identity.save(update_fields=["last_login_at"])

        if org is None and org_features_enabled():
            membership = (
                user.org_memberships.select_related("organization")
                .order_by("created_at")
                .first()
            )
            org = membership.organization if membership else None

        audit_log(
            "user.login",
            user=user,
            action="login",
            resource_type="user",
            resource_id=str(user.id),
            metadata={"method": "entra_oauth"},
        )

        return {
            "user": user,
            "org": org,
            "joined_org": joined_org,
            "is_new_user": is_new_user,
        }
