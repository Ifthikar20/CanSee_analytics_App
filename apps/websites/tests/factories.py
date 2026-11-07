import factory
from django.utils import timezone
from factory.django import DjangoModelFactory

from apps.accounts.tests.factories import UserFactory
from apps.analytics.models import PageEvent, Visitor
from apps.websites.models import Website, WebsiteSettings


class WebsiteFactory(DjangoModelFactory):
    class Meta:
        model = Website

    user = factory.SubFactory(UserFactory)
    url = factory.Sequence(lambda n: f"https://site{n}.example.com")
    name = factory.Sequence(lambda n: f"Site {n}")
    is_active = True

    @factory.post_generation
    def settings(obj, create, extracted, **kwargs):
        if create:
            WebsiteSettings.objects.create(website=obj)


class VisitorFactory(DjangoModelFactory):
    class Meta:
        model = Visitor

    website = factory.SubFactory(WebsiteFactory)
    fingerprint_hash = factory.Sequence(lambda n: f"fp{n:064d}")
    geo_country = "US"
    device_type = "desktop"
    visit_count = 1
    lead_score = 0


class PageEventFactory(DjangoModelFactory):
    class Meta:
        model = PageEvent

    visitor = factory.SubFactory(VisitorFactory)
    website = factory.LazyAttribute(lambda o: o.visitor.website)
    url = "https://example.com/page"
    event_type = "pageview"
    timestamp = factory.LazyFunction(timezone.now)
