"""Tests for layered country resolution."""
from __future__ import annotations

from types import SimpleNamespace

from django.test import TestCase, override_settings

from apps.analytics.services.geo import resolve_country


def _req(**headers):
    return SimpleNamespace(META=dict(headers))


class GeoResolveTest(TestCase):
    def test_trusted_edge_header_wins(self):
        req = _req(HTTP_CF_IPCOUNTRY="de")
        # Even with a public IP and a different client claim, the edge header
        # is authoritative.
        self.assertEqual(
            resolve_country(request=req, ip="8.8.8.8", client_value="us"), "DE",
        )

    def test_header_normalised_uppercase(self):
        self.assertEqual(resolve_country(request=_req(HTTP_CF_IPCOUNTRY="gb")), "GB")

    def test_cloudflare_unknown_codes_ignored(self):
        # XX (unknown) and T1 (Tor) are not real countries.
        self.assertEqual(resolve_country(request=_req(HTTP_CF_IPCOUNTRY="XX")), "")
        self.assertEqual(resolve_country(request=_req(HTTP_CF_IPCOUNTRY="T1")), "")

    @override_settings(GEO_COUNTRY_HEADERS=("HTTP_X_GEO_COUNTRY",))
    def test_custom_header_name(self):
        self.assertEqual(
            resolve_country(request=_req(HTTP_X_GEO_COUNTRY="fr")), "FR",
        )

    def test_client_value_used_only_as_last_resort(self):
        # No header, private IP (skips maxmind + ip-api), so the client hint
        # is all that's left.
        self.assertEqual(
            resolve_country(request=_req(), ip="127.0.0.1", client_value="jp"), "JP",
        )

    def test_invalid_client_value_rejected(self):
        self.assertEqual(
            resolve_country(request=_req(), ip="10.0.0.1", client_value="JAPAN"), "",
        )

    def test_empty_when_nothing_resolves(self):
        self.assertEqual(
            resolve_country(request=_req(), ip="192.168.1.1", client_value=""), "",
        )
