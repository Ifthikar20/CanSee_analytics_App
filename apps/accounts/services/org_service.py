"""Organization membership, invitation, and enforcement business logic.

Views stay thin; everything stateful happens here. Token rules:

- An invitation's raw token is minted here, handed to the email task, and
  never stored — the DB holds sha256(token) only.
- Accepting is idempotent-safe: expired/revoked/used tokens all fail the
  same way (not found), so the endpoint can't be used as a token oracle.
"""
import hashlib
import secrets
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.accounts.models import (
    Invitation,
    Organization,
    OrganizationMember,
    User,
)
from core.exceptions import CanseeException, PermissionDenied, ResourceNotFound
from core.logging.audit_logger import audit_log
from core.utils.constants import OrgRole

INVITATION_TTL_DAYS = 7


class AlreadyInOrganization(CanseeException):
    def __init__(self):
        super().__init__(
            "This account already belongs to an organization.",
            code="already_in_org",
            status_code=409,
        )


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _pending_invitations(qs=None):
    qs = qs if qs is not None else Invitation.objects.all()
    return qs.filter(
        accepted_at__isnull=True,
        revoked_at__isnull=True,
        expires_at__gt=timezone.now(),
    )


class OrgService:
    # ── Membership ────────────────────────────────────────────────

    @staticmethod
    def membership_for(user) -> OrganizationMember | None:
        return (
            user.org_memberships.select_related("organization")
            .order_by("created_at")
            .first()
        )

    @staticmethod
    def add_member(
        *, organization: Organization, user: User, role: str,
        invited_by: User | None = None, joined_via: str = "invite",
    ) -> OrganizationMember:
        """Create a membership, enforcing the one-org-per-user rule and
        the org's seat cap.

        Every path that adds a person is seat-gated here. An accepted
        invitation (``joined_via="invite"``) already reserved its seat when
        it was created, so it only needs the members to be under the cap;
        JIT joins (Workspace domain auto-join and SAML provisioning) and
        operator adds need a free seat on top of the pending invitations,
        because auto-join is an unbounded door: without this check a big
        Workspace or IdP directory could blow straight past the seat count.
        An org that is over its cap after a plan change keeps its members
        and cannot add more. The org row is locked so two concurrent joins
        cannot both take the last seat.
        """
        from apps.billing.services.org_entitlements import (
            seat_available,
            seat_limit_for,
        )

        with transaction.atomic():
            # Lock the user row so a concurrent invite-accept and domain-JIT
            # join can't both slip past the membership check.
            User.objects.select_for_update().get(pk=user.pk)
            existing = user.org_memberships.select_related("organization").first()
            if existing:
                if existing.organization_id == organization.id:
                    return existing
                raise AlreadyInOrganization()
            Organization.objects.select_for_update().filter(pk=organization.pk).first()
            if not seat_available(organization, holding_invitation=joined_via == "invite"):
                seat_cap = seat_limit_for(organization)
                if joined_via == "invite":
                    raise CanseeException(
                        f"All {seat_cap} seats in {organization.name} are taken. "
                        "Ask your workspace admin to free a seat.",
                        code="plan_limit_exceeded",
                        status_code=403,
                        details={"limit": seat_cap},
                    )
                raise CanseeException(
                    f"{organization.name} has no seats available. Ask your "
                    "workspace admin to add seats or invite you directly.",
                    code="no_seats_available",
                    status_code=403,
                    details={"limit": seat_cap},
                )
            member = OrganizationMember.objects.create(
                organization=organization,
                user=user,
                role=role,
                invited_by=invited_by,
                joined_via=joined_via,
            )
        audit_log(
            "org.member_added",
            user=invited_by or user,
            action="create",
            resource_type="organization_member",
            resource_id=str(member.id),
            metadata={"org": str(organization.id), "role": role, "via": joined_via},
        )
        return member

    @staticmethod
    def change_role(*, acting: OrganizationMember, member_id: int, role: str) -> OrganizationMember:
        if role not in {OrgRole.ADMIN, OrgRole.MEMBER, OrgRole.VIEWER}:
            raise PermissionDenied("That role can't be granted.")
        try:
            member = OrganizationMember.objects.select_related("user").get(
                id=member_id, organization_id=acting.organization_id
            )
        except OrganizationMember.DoesNotExist:
            raise ResourceNotFound("Member not found.") from None

        if member.role == OrgRole.OWNER:
            raise PermissionDenied("The owner's role can't be changed.")
        if member.user_id == acting.user_id:
            raise PermissionDenied("You can't change your own role.")
        # Only the owner may grant or revoke admin.
        if (
            OrgRole.ADMIN in (role, member.role)
            and acting.role != OrgRole.OWNER
        ):
            raise PermissionDenied("Only the owner can grant or revoke admin.")

        member.role = role
        member.save(update_fields=["role"])
        audit_log(
            "org.member_role_changed",
            user=acting.user,
            action="update",
            resource_type="organization_member",
            resource_id=str(member.id),
            metadata={"role": role},
        )
        return member

    @staticmethod
    def remove_member(*, acting: OrganizationMember, member_id: int) -> None:
        try:
            member = OrganizationMember.objects.select_related("user").get(
                id=member_id, organization_id=acting.organization_id
            )
        except OrganizationMember.DoesNotExist:
            raise ResourceNotFound("Member not found.") from None

        if member.role == OrgRole.OWNER:
            raise PermissionDenied("The owner can't be removed.")
        if member.user_id == acting.user_id:
            raise PermissionDenied("Leave the organization from your own account settings.")
        if member.role == OrgRole.ADMIN and acting.role != OrgRole.OWNER:
            raise PermissionDenied("Only the owner can remove an admin.")

        from apps.accounts.services.token_service import TokenService

        removed_user_id = member.user_id
        member.delete()
        # Their sessions die with the membership — org claims in a live
        # refresh token must not outlive it.
        TokenService.revoke_all_for_users([removed_user_id])
        audit_log(
            "org.member_removed",
            user=acting.user,
            action="delete",
            resource_type="organization_member",
            resource_id=str(member_id),
            metadata={"removed_user": str(removed_user_id)},
        )

    # ── Invitations ───────────────────────────────────────────────

    @staticmethod
    def create_invitation(
        *, acting: OrganizationMember, email: str, role: str
    ) -> Invitation:
        email = (email or "").strip().lower()
        if not email or "@" not in email:
            raise ValueError("Enter a valid email address.")
        if role not in {OrgRole.ADMIN, OrgRole.MEMBER, OrgRole.VIEWER}:
            raise ValueError("Invitations can grant admin, member, or viewer.")
        if role == OrgRole.ADMIN and acting.role != OrgRole.OWNER:
            raise PermissionDenied("Only the owner can invite admins.")

        org = acting.organization
        existing_user = User.objects.filter(email__iexact=email).first()
        if existing_user and existing_user.org_memberships.exists():
            raise AlreadyInOrganization()
        if _pending_invitations(org.invitations).filter(email=email).exists():
            raise CanseeException(
                "That address already has a pending invitation — resend it instead.",
                code="already_invited",
                status_code=400,
            )
        # The uniq_pending_invite constraint can't see expiry (a partial
        # unique index has no notion of "now"), so an EXPIRED invite for
        # this email still occupies the slot — retire it before creating.
        org.invitations.filter(
            email=email,
            accepted_at__isnull=True,
            revoked_at__isnull=True,
            expires_at__lte=timezone.now(),
        ).update(revoked_at=timezone.now())

        # Seat gate: seats are the billing unit — members plus pending
        # invites occupy them. Custom org packages override the plan's
        # team_members number. An org over its cap after a plan change
        # keeps its members but cannot invite.
        from apps.billing.services.org_entitlements import (
            seat_available,
            seat_limit_for,
            seats_used,
        )

        seat_cap = seat_limit_for(org)
        if not seat_available(org):
            raise CanseeException(
                f"All {seat_cap} seats are taken. Remove a member, revoke a "
                "pending invitation, or contact us to add seats.",
                code="plan_limit_exceeded",
                status_code=403,
                details={"used": seats_used(org), "limit": seat_cap},
            )

        raw_token = secrets.token_urlsafe(32)
        invitation = Invitation.objects.create(
            organization=org,
            email=email,
            role=role,
            token_hash=_hash_token(raw_token),
            invited_by=acting.user,
            expires_at=timezone.now() + timedelta(days=INVITATION_TTL_DAYS),
        )

        from apps.accounts.tasks import send_org_invitation_email
        send_org_invitation_email.delay(
            email, org.name, acting.user.full_name, role, raw_token
        )
        audit_log(
            "org.invitation_created",
            user=acting.user,
            action="create",
            resource_type="invitation",
            resource_id=str(invitation.id),
            metadata={"role": role},
        )
        return invitation

    @staticmethod
    def resend_invitation(*, acting: OrganizationMember, invitation_id) -> Invitation:
        try:
            invitation = Invitation.objects.get(
                id=invitation_id,
                organization_id=acting.organization_id,
                accepted_at__isnull=True,
                revoked_at__isnull=True,
            )
        except Invitation.DoesNotExist:
            raise ResourceNotFound("Invitation not found.") from None

        # A live invitation already holds its seat. An expired one gave it
        # up (seats_used counts live invitations only), so reviving it takes
        # a seat exactly like a new invitation would.
        if invitation.expires_at <= timezone.now():
            from apps.billing.services.org_entitlements import (
                seat_available,
                seat_limit_for,
                seats_used,
            )

            org = acting.organization
            if not seat_available(org):
                seat_cap = seat_limit_for(org)
                raise CanseeException(
                    f"All {seat_cap} seats are taken. Remove a member, revoke a "
                    "pending invitation, or contact us to add seats.",
                    code="plan_limit_exceeded",
                    status_code=403,
                    details={"used": seats_used(org), "limit": seat_cap},
                )

        # Rotate the token so an old email's link stops working.
        raw_token = secrets.token_urlsafe(32)
        invitation.token_hash = _hash_token(raw_token)
        invitation.expires_at = timezone.now() + timedelta(days=INVITATION_TTL_DAYS)
        invitation.save(update_fields=["token_hash", "expires_at"])

        from apps.accounts.tasks import send_org_invitation_email
        send_org_invitation_email.delay(
            invitation.email,
            acting.organization.name,
            acting.user.full_name,
            invitation.role,
            raw_token,
        )
        return invitation

    @staticmethod
    def revoke_invitation(*, acting: OrganizationMember, invitation_id) -> None:
        try:
            invitation = Invitation.objects.get(
                id=invitation_id,
                organization_id=acting.organization_id,
                accepted_at__isnull=True,
                revoked_at__isnull=True,
            )
        except Invitation.DoesNotExist:
            raise ResourceNotFound("Invitation not found.") from None
        invitation.revoked_at = timezone.now()
        invitation.save(update_fields=["revoked_at"])

    @staticmethod
    def invitation_by_token(raw_token: str) -> Invitation:
        """Resolve a pending invitation from its raw token, or 404."""
        try:
            invitation = (
                _pending_invitations()
                .select_related("organization", "invited_by")
                .get(token_hash=_hash_token(raw_token or ""))
            )
        except Invitation.DoesNotExist:
            raise ResourceNotFound("This invitation is no longer valid.") from None
        return invitation

    @staticmethod
    def accept_invitation_existing(*, raw_token: str, user: User) -> OrganizationMember:
        """An authenticated, existing account accepts its invitation."""
        invitation = OrgService.invitation_by_token(raw_token)
        if user.email.lower() != invitation.email:
            raise CanseeException(
                "This invitation was sent to a different email address.",
                code="invitation_email_mismatch",
                status_code=403,
            )
        member = OrgService.add_member(
            organization=invitation.organization,
            user=user,
            role=invitation.role,
            invited_by=invitation.invited_by,
            joined_via="invite",
        )
        invitation.accepted_at = timezone.now()
        invitation.save(update_fields=["accepted_at"])
        return member

    @staticmethod
    def register_via_invitation(
        *, raw_token: str, full_name: str, password: str,
        ip_address: str = "", user_agent: str = "",
    ) -> dict:
        """Create the account an invitation was addressed to, join, and log in.

        This is the ONLY path that creates a user while SIGNUPS_ENABLED is
        False, and it deliberately never touches RegisterView: possession
        of the emailed single-use token both authorizes the signup and
        proves control of the mailbox (hence is_email_verified=True — the
        OTP flow would re-verify what the invite email already did).
        """
        from apps.accounts.services.auth_service import AuthService
        from apps.accounts.services.token_service import TokenService

        invitation = OrgService.invitation_by_token(raw_token)

        if User.objects.filter(email__iexact=invitation.email).exists():
            raise CanseeException(
                "An account with this email already exists — sign in to accept.",
                code="user_exists",
                status_code=409,
            )

        AuthService._enforce_password_policy(password)
        # Atomic: a seat-cap refusal inside add_member must not leave an
        # account behind that would then block the retry as "user_exists".
        with transaction.atomic():
            user = User.objects.create_user(
                email=invitation.email,
                password=password,
                full_name=full_name,
                company_name=invitation.organization.name,
                is_email_verified=True,
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

        result = TokenService.issue_session(
            user, method="invite", ip_address=ip_address, user_agent=user_agent
        )
        result["org"] = {
            "id": str(invitation.organization_id),
            "name": invitation.organization.name,
        }
        return result

    # ── SSO enforcement ───────────────────────────────────────────

    @staticmethod
    def set_sso_enforcement(
        *, acting: OrganizationMember, enabled: bool, acting_jti: str | None = None
    ) -> int:
        """Flip require_sso. Returns how many sessions were revoked.

        Preconditions on enable guard against self-lockout: the org needs a
        verified domain, and the owner must already have a linked IdP
        identity — otherwise the flip would block the only credential the
        owner has.
        """
        org = acting.organization
        if enabled == org.require_sso:
            return 0

        revoked = 0
        if enabled:
            if not org.domains.filter(verified_at__isnull=False).exists():
                raise CanseeException(
                    "Verify a company domain before requiring SSO.",
                    code="domain_not_verified",
                    status_code=400,
                )
            owner_has_idp = org.owner.social_identities.exists()
            if not owner_has_idp:
                raise CanseeException(
                    "The organization owner must sign in with the identity "
                    "provider once before SSO can be required.",
                    code="owner_idp_unlinked",
                    status_code=400,
                )
            org.require_sso = True
            org.save(update_fields=["require_sso"])

            # Kill every member session except the acting one — password
            # sessions must re-authenticate through the IdP immediately,
            # not when their refresh tokens happen to expire.
            from apps.accounts.services.token_service import TokenService
            member_ids = list(org.members.values_list("user_id", flat=True))
            revoked = TokenService.revoke_all_for_users(
                member_ids, exclude_jti=acting_jti
            )
        else:
            org.require_sso = False
            org.save(update_fields=["require_sso"])

        audit_log(
            "org.sso_enforcement_changed",
            user=acting.user,
            action="update",
            resource_type="organization",
            resource_id=str(org.id),
            metadata={"enabled": enabled, "sessions_revoked": revoked},
        )
        return revoked
