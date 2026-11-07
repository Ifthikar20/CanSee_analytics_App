"""Unit tests for the hosted Google-tag source (pool property streams)."""

from unittest.mock import MagicMock, patch

import pytest

from apps.web_analytics.services import ga4_hosted
from apps.web_analytics.tests.factories import HostedIntegrationFactory
from apps.websites.models import Integration
from apps.websites.tests.factories import WebsiteFactory


@pytest.fixture(autouse=True)
def _configured(settings):
    settings.GA4_SA_CREDENTIALS_JSON = '{"client_email": "sa@x", "private_key": "k"}'
    settings.GA4_HOSTED_PROPERTY_ID = "555555"


def _stub_response(payload, status_code=200, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.json = MagicMock(return_value=payload)
    return resp


def test_snippet_contains_measurement_id():
    out = ga4_hosted.snippet("G-ABC123")
    assert 'src="https://www.googletagmanager.com/gtag/js?id=G-ABC123"' in out
    assert "gtag('config','G-ABC123')" in out


@pytest.mark.django_db
def test_provision_creates_stream_and_stores_ids():
    website = WebsiteFactory(url="https://example.com")
    created = _stub_response({
        "name": "properties/555555/dataStreams/987",
        "webStreamData": {"measurementId": "G-NEW999"},
    })
    with patch.object(ga4_hosted.ga4_service_account, "get_access_token", return_value="sa-tok"), \
         patch.object(ga4_hosted.requests, "post", return_value=created) as mock_post:
        integration, error = ga4_hosted.provision_stream(website)

    assert error is None
    assert integration.is_active is True
    assert integration.metadata["measurement_id"] == "G-NEW999"
    assert integration.metadata["stream_id"] == "987"
    body = mock_post.call_args.kwargs["json"]
    assert body["type"] == "WEB_DATA_STREAM"
    assert body["webStreamData"]["defaultUri"] == "https://example.com"


@pytest.mark.django_db
def test_provision_is_idempotent():
    integration = HostedIntegrationFactory(is_active=False)
    with patch.object(ga4_hosted.requests, "post") as mock_post:
        out, error = ga4_hosted.provision_stream(integration.website)

    mock_post.assert_not_called()
    assert error is None
    assert out.pk == integration.pk
    assert out.is_active is True


@pytest.mark.django_db
def test_provision_surfaces_stream_limit():
    website = WebsiteFactory()
    rejected = _stub_response({}, status_code=400, text="maximum number of data streams reached")
    with patch.object(ga4_hosted.ga4_service_account, "get_access_token", return_value="sa-tok"), \
         patch.object(ga4_hosted.requests, "post", return_value=rejected):
        integration, error = ga4_hosted.provision_stream(website)

    assert error == "stream_limit_reached"
    assert integration.is_active is False
    assert integration.metadata["provisioning_error"] == "stream_limit_reached"


@pytest.mark.django_db
def test_provision_without_sa_token():
    website = WebsiteFactory()
    with patch.object(ga4_hosted.ga4_service_account, "get_access_token", return_value=None):
        integration, error = ga4_hosted.provision_stream(website)
    assert error == "sa_token_failed"
    assert Integration.objects.filter(website=website, type="ga_hosted").exists()


@pytest.mark.django_db
def test_disable_deletes_stream_and_clears_ids():
    integration = HostedIntegrationFactory()
    with patch.object(ga4_hosted.ga4_service_account, "get_access_token", return_value="sa-tok"), \
         patch.object(ga4_hosted.requests, "delete") as mock_delete:
        ga4_hosted.disable(integration)

    mock_delete.assert_called_once()
    assert "dataStreams/987654" in mock_delete.call_args[0][0]
    integration.refresh_from_db()
    assert integration.is_active is False
    assert "measurement_id" not in integration.metadata
    assert "stream_id" not in integration.metadata


@pytest.mark.django_db
def test_hosted_snapshot_filters_by_stream():
    integration = HostedIntegrationFactory()
    with patch.object(ga4_hosted.ga4_service_account, "get_access_token", return_value="sa-tok"), \
         patch.object(ga4_hosted.ga4_client, "build_realtime_snapshot", return_value={"x": 1}) as mock_build:
        out = ga4_hosted.build_hosted_snapshot(integration.website, integration)

    assert out == {"x": 1}
    kwargs = mock_build.call_args.kwargs
    assert kwargs["source"] == "ga_hosted"
    assert kwargs["dimension_filter"]["filter"]["stringFilter"]["value"] == "987654"
    assert mock_build.call_args.args[1] == "555555"
