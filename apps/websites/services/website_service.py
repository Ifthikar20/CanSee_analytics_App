import logging

from django.core.exceptions import ValidationError
from django.db.models import Q

from apps.websites.models import Website, WebsiteSettings
from core.exceptions import DuplicateWebsite, PermissionDenied, ResourceNotFound
from core.logging.audit_logger import audit_log
from core.permissions.rbac import denied_message, role_at_least
from core.validators.url_validator import validate_website_url

logger = logging.getLogger("apps")


def _org_id_for(user):
    """The user's single org id, cached on the user object. None = org-less."""
    if not hasattr(user, "_org_id_cache"):
        membership = (
            user.org_memberships.order_by("created_at")
            .values_list("organization_id", flat=True)
            .first()
        )
        user._org_id_cache = membership
    return user._org_id_cache


class WebsiteService:
    @staticmethod
    def accessible_qs(user):
        """Every website this user may act on: their personal ones + their org's.

        THE tenancy queryset — list endpoints, project caps, and
        needs_onboarding must all derive from this, never from
        ``filter(user=...)``, or org members see a different world than
        get_for_user grants them.

        Org-owned websites are reached only through a current membership:
        creating a project inside an organization does not keep it in the
        creator's list once they leave (or are removed from) that org.
        """
        personal = Q(user=user, organization__isnull=True)
        org_id = _org_id_for(user)
        if org_id is None:
            return Website.objects.filter(personal)
        return Website.objects.filter(personal | Q(organization_id=org_id))

    @staticmethod
    def created_by_qs(user):
        """Websites this user created that they can still reach.

        For per-user features (digests, SMS answers) that used to read
        ``filter(user=user)``: an org project drops out the moment its
        creator is removed from the organization.
        """
        return WebsiteService.accessible_qs(user).filter(user=user)

    @staticmethod
    def created_by_qs_with_role(user, min_role: str):
        """created_by_qs narrowed to websites where the user's CURRENT role
        meets ``min_role``.

        For per-user surfaces outside the request cycle (chat commands, SMS)
        that change data or spend engine runs: an org project its creator
        was downgraded on drops out, the same as get_for_user refusing it.
        Personal websites always stay (their creator is their owner). The
        org comes from the user's membership (_org_id_for), so its role is
        that membership row's role, as in role_for.
        """
        qs = WebsiteService.created_by_qs(user)
        org_id = _org_id_for(user)
        if org_id is None:
            return qs
        role = (
            user.org_memberships.filter(organization_id=org_id)
            .values_list("role", flat=True)
            .first()
        )
        if role and role_at_least(role, min_role):
            return qs
        return qs.filter(organization__isnull=True)

    @staticmethod
    def role_for(user, website) -> str | None:
        """The user's current role on ``website``, re-read on every call.

        A personal website (no organization) belongs to its creator, who is
        its "owner". An org website goes by the user's membership in that
        organization (or by being the organization's owner); who created
        the website never matters, so removing or downgrading a member takes
        effect on their next request.
        """
        if not website.organization_id:
            return "owner" if website.user_id == user.id else None
        role = (
            user.org_memberships.filter(organization_id=website.organization_id)
            .values_list("role", flat=True)
            .first()
        )
        if role:
            return role
        # The organization's owner cannot be removed or downgraded
        # (OrgService), so the owner field stands in for a missing row.
        if website.organization.owner_id == user.id:
            return "owner"
        return None

    @staticmethod
    def create(*, user, url: str, name: str, industry: str = "", platform_type: str = "custom") -> Website:
        """Add a new website for a user (org-owned when the user has an org)."""
        validated_url = validate_website_url(url)
        org_id = _org_id_for(user)
        if org_id is not None:
            role = (
                user.org_memberships.filter(organization_id=org_id)
                .values_list("role", flat=True)
                .first()
            )
            if not role_at_least(role or "", "member"):
                raise PermissionDenied(
                    "Your organization role can view projects but not add them."
                )

        # An active duplicate would otherwise die on the tenant-scoped
        # unique constraint as a 500. Scope matches the constraint: the
        # org's projects for org users, the user's own otherwise.
        duplicate_scope = (
            Website.objects.filter(organization_id=org_id, url=validated_url)
            if org_id
            else Website.objects.filter(user=user, organization__isnull=True, url=validated_url)
        )
        if duplicate_scope.exists():
            raise DuplicateWebsite()

        # Check if a soft-deleted website with the same URL exists — restore it
        deleted_scope = (
            Website.all_objects.filter(organization_id=org_id, url=validated_url, is_deleted=True)
            if org_id
            else Website.all_objects.filter(user=user, organization__isnull=True, url=validated_url, is_deleted=True)
        )
        existing = deleted_scope.first()
        if existing:
            existing.is_deleted = False
            existing.deleted_at = None
            existing.deleted_by = None
            existing.name = name
            existing.industry = industry or existing.industry
            existing.platform_type = platform_type
            existing.save()
            audit_log("website.restored", user=user, action="update", resource_type="website", resource_id=str(existing.id), metadata={"url": validated_url})
            return existing

        website = Website.objects.create(
            user=user, organization_id=org_id, url=validated_url, name=name,
            industry=industry, platform_type=platform_type,
        )
        WebsiteSettings.objects.create(website=website)
        audit_log("website.created", user=user, action="create", resource_type="website", resource_id=str(website.id), metadata={"url": validated_url})
        return website

    @staticmethod
    def get_for_user(*, user, website_id: str, min_role: str = "member") -> Website:
        """Fetch a website the user owns or can reach through org membership.

        The single tenancy gate: ~160 call sites across every app resolve
        website access through here. Users with no role on the website get
        the same 404 as a nonexistent id, so existence does not leak.
        ``min_role`` is checked against role_for on the ladder in
        core.permissions.rbac (owner > admin > member > viewer). A current
        org member below ``min_role`` already sees the project in their
        list, so they get a 403 that says why instead of a 404.
        """
        try:
            website = Website.objects.select_related("organization").get(id=website_id)
        except (Website.DoesNotExist, ValidationError, ValueError):
            raise ResourceNotFound("Website not found.") from None

        role = WebsiteService.role_for(user, website)
        if not role:
            raise ResourceNotFound("Website not found.") from None
        if not role_at_least(role, min_role):
            raise PermissionDenied(denied_message(min_role))
        return website

    @staticmethod
    def update(*, website: Website, user, **kwargs) -> Website:
        allowed_fields = {"name", "industry", "description", "topics", "competitors"}
        if "competitors" in kwargs:
            # Stored as plain [{name, domain}] dicts, the shape every reader expects.
            kwargs["competitors"] = [
                {"name": c["name"].strip(), "domain": (c.get("domain") or "").strip().lower()}
                for c in kwargs["competitors"] if c.get("name", "").strip()
            ]
        for field in allowed_fields & kwargs.keys():
            setattr(website, field, kwargs[field])
        website.save()
        audit_log("website.updated", user=user, action="update", resource_type="website", resource_id=str(website.id), metadata={"fields": list(kwargs.keys())})
        return website

    @staticmethod
    def delete(*, website: Website, user) -> None:
        website.soft_delete(user=user)
        audit_log("website.deleted", user=user, action="delete", resource_type="website", resource_id=str(website.id))

    @staticmethod
    def regenerate_pixel_key(*, website: Website, user) -> Website:
        import uuid
        website.pixel_key = uuid.uuid4()
        website.pixel_verified = False
        website.pixel_verified_at = None
        website.save(update_fields=["pixel_key", "pixel_verified", "pixel_verified_at"])
        audit_log("website.pixel_regenerated", user=user, action="update", resource_type="website", resource_id=str(website.id))
        return website
