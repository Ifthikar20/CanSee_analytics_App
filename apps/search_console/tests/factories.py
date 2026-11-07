import datetime

import factory
from factory.django import DjangoModelFactory

from apps.search_console.models import GscDailyTotal, GscPageStat, GscQueryStat
from apps.websites.models import Integration
from apps.websites.tests.factories import WebsiteFactory


class GscIntegrationFactory(DjangoModelFactory):
    class Meta:
        model = Integration

    website = factory.SubFactory(WebsiteFactory)
    type = "gsc"
    access_token = "access-token"
    refresh_token = "refresh-token"
    is_active = True
    metadata = factory.LazyFunction(lambda: {"site_url": "sc-domain:example.com"})


class GscDailyTotalFactory(DjangoModelFactory):
    class Meta:
        model = GscDailyTotal

    website = factory.SubFactory(WebsiteFactory)
    date = factory.Sequence(
        lambda n: datetime.date(2026, 1, 1) + datetime.timedelta(days=n % 365)
    )
    clicks = 10
    impressions = 100
    ctr = 0.1
    position = 5.0


class GscQueryStatFactory(DjangoModelFactory):
    class Meta:
        model = GscQueryStat

    website = factory.SubFactory(WebsiteFactory)
    date = factory.Sequence(
        lambda n: datetime.date(2026, 1, 1) + datetime.timedelta(days=n % 365)
    )
    query = factory.Sequence(lambda n: f"how to choose product {n}")
    clicks = 5
    impressions = 50
    ctr = 0.1
    position = 8.0


class GscPageStatFactory(DjangoModelFactory):
    class Meta:
        model = GscPageStat

    website = factory.SubFactory(WebsiteFactory)
    date = factory.Sequence(
        lambda n: datetime.date(2026, 1, 1) + datetime.timedelta(days=n % 365)
    )
    page = factory.Sequence(lambda n: f"https://example.com/page-{n}")
    clicks = 5
    impressions = 50
    ctr = 0.1
    position = 8.0
