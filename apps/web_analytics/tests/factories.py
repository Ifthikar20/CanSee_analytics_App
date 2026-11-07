import factory
from factory.django import DjangoModelFactory

from apps.websites.models import Integration
from apps.websites.tests.factories import WebsiteFactory


class Ga4IntegrationFactory(DjangoModelFactory):
    class Meta:
        model = Integration

    website = factory.SubFactory(WebsiteFactory)
    type = "ga"
    access_token = "access-token"
    refresh_token = "refresh-token"
    is_active = True
    metadata = factory.LazyFunction(
        lambda: {"property_id": "123456", "property_display_name": "Example — GA4"}
    )


class HostedIntegrationFactory(DjangoModelFactory):
    class Meta:
        model = Integration

    website = factory.SubFactory(WebsiteFactory)
    type = "ga_hosted"
    is_active = True
    metadata = factory.LazyFunction(
        lambda: {
            "measurement_id": "G-TEST1234",
            "stream_id": "987654",
            "property_id": "555555",
        }
    )


class CloudflareIntegrationFactory(DjangoModelFactory):
    class Meta:
        model = Integration

    website = factory.SubFactory(WebsiteFactory)
    type = "cloudflare"
    access_token = "cf-token"
    is_active = True
    metadata = factory.LazyFunction(
        lambda: {
            "zone_id": "023e105f4ecef8ad9ca31a8372d0c353",
            "zone_name": "example.com",
            "available_zones": [
                {"id": "023e105f4ecef8ad9ca31a8372d0c353", "name": "example.com",
                 "status": "active", "paused": False}
            ],
        }
    )
