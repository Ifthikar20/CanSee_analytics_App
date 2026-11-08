from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from apps.websites.models import Website, WebsiteMembership, WebsiteSettings
from core.validators.url_validator import validate_website_url


class WebsiteSerializer(serializers.ModelSerializer):
    pixel_snippet = serializers.SerializerMethodField()
    created_by = serializers.SerializerMethodField()

    class Meta:
        model = Website
        fields = [
            "id", "url", "name", "industry", "description", "topics", "competitors",
            "platform_type",
            "pixel_key", "pixel_verified", "pixel_verified_at", "crawl_status",
            "is_active", "pixel_snippet", "created_by", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "pixel_key", "pixel_verified", "pixel_verified_at", "crawl_status"]

    def get_pixel_snippet(self, obj):
        from apps.websites.services.pixel_service import PixelService

        return PixelService.get_snippet(website=obj)

    def get_created_by(self, obj) -> dict | None:
        # Attribution for shared org projects ("Added by Sarah").
        if not obj.user_id:
            return None
        return {"id": str(obj.user_id), "full_name": obj.user.full_name}


class WebsiteCreateSerializer(serializers.Serializer):
    # CharField, not URLField: users type "example.com" without a scheme,
    # and validate_website_url (the same validator the service runs) both
    # normalizes that to https:// and blocks localhost/private hosts.
    url = serializers.CharField(max_length=500)
    name = serializers.CharField(max_length=200)
    industry = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    platform_type = serializers.ChoiceField(
        choices=["shopify", "wordpress", "woocommerce", "custom"],
        required=False, default="custom"
    )

    def validate_url(self, value):
        try:
            return validate_website_url(value.strip())
        except DjangoValidationError as e:
            raise serializers.ValidationError(e.messages[0]) from None


class CompetitorSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200)
    domain = serializers.CharField(max_length=300, required=False, allow_blank=True, default="")


class WebsiteUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200, required=False)
    # Blank is a normal stored value (the model, the create serializer and
    # onboarding all allow it), so an update must accept it too; otherwise
    # a project created without an industry can never be saved.
    industry = serializers.CharField(max_length=100, required=False, allow_blank=True)
    description = serializers.CharField(required=False, allow_blank=True)
    # No default: an update that leaves a list out must not wipe it.
    topics = serializers.ListField(
        child=serializers.CharField(max_length=200),
        required=False,
    )
    competitors = CompetitorSerializer(many=True, required=False, max_length=25)


class WebsiteSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = WebsiteSettings
        fields = ["track_anonymous", "weekly_report"]


class WebsiteMembershipSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source="user.email", read_only=True)
    user_name = serializers.CharField(source="user.full_name", read_only=True)

    class Meta:
        model = WebsiteMembership
        fields = ["id", "user_email", "user_name", "role", "accepted", "created_at"]
        read_only_fields = ["id", "user_email", "user_name", "accepted", "created_at"]
