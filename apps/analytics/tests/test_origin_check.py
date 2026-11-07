"""Tests for the pixel-ingest Origin allowlist."""
from __future__ import annotations

from types import SimpleNamespace

from django.test import RequestFactory, TestCase, override_settings

from apps.analytics.services.origin_check import is_origin_allowed


def _req(origin=None, referer=None):
    headers = {}
    if origin is not None:
        headers["HTTP_ORIGIN"] = origin
    if referer is not None:
        headers["HTTP_REFERER"] = referer
    return RequestFactory().post("/api/v1/track/event/", **headers)


def _site(url):
    return SimpleNamespace(url=url)


@override_settings(DEBUG=False)
class OriginAllowlistTest(TestCase):
    def test_allows_exact_match(self):
        self.assertTrue(is_origin_allowed(
            _req(origin="https://outfi.ai/cart"), _site("https://outfi.ai"),
        ))

    def test_treats_www_and_apex_as_equivalent(self):
        self.assertTrue(is_origin_allowed(
            _req(origin="https://www.outfi.ai/"), _site("https://outfi.ai"),
        ))
        self.assertTrue(is_origin_allowed(
            _req(origin="https://outfi.ai/"), _site("https://www.outfi.ai"),
        ))

    def test_allows_any_subdomain_of_registered_apex(self):
        self.assertTrue(is_origin_allowed(
            _req(origin="https://shop.outfi.ai/"), _site("https://outfi.ai"),
        ))
        self.assertTrue(is_origin_allowed(
            _req(origin="https://eu.shop.outfi.ai/"), _site("https://www.outfi.ai"),
        ))

    def test_ignores_port_differences(self):
        self.assertTrue(is_origin_allowed(
            _req(origin="https://outfi.ai:8443/x"), _site("https://outfi.ai"),
        ))

    def test_rejects_different_apex(self):
        self.assertFalse(is_origin_allowed(
            _req(origin="https://outfi-thief.ai/"), _site("https://outfi.ai"),
        ))
        self.assertFalse(is_origin_allowed(
            # Naive endswith check would have allowed this — suffix is
            # ".outfi.ai" only when preceded by "."
            _req(origin="https://evil-outfi.ai/"), _site("https://outfi.ai"),
        ))

    def test_falls_back_to_referer_when_origin_missing(self):
        # sendBeacon can post without an Origin header on some browsers,
        # so we accept Referer as a secondary signal.
        self.assertTrue(is_origin_allowed(
            _req(referer="https://outfi.ai/"), _site("https://outfi.ai"),
        ))

    def test_rejects_when_both_headers_missing(self):
        self.assertFalse(is_origin_allowed(_req(), _site("https://outfi.ai")))

    def test_rejects_when_website_has_no_url(self):
        self.assertFalse(is_origin_allowed(
            _req(origin="https://outfi.ai/"), _site(""),
        ))

    @override_settings(DEBUG=True)
    def test_debug_mode_allows_localhost(self):
        self.assertTrue(is_origin_allowed(
            _req(origin="http://localhost:5173/"), _site("https://outfi.ai"),
        ))
        self.assertTrue(is_origin_allowed(
            _req(origin="http://127.0.0.1:8000/"), _site("https://outfi.ai"),
        ))

    def test_localhost_blocked_when_debug_off(self):
        self.assertFalse(is_origin_allowed(
            _req(origin="http://localhost:5173/"), _site("https://outfi.ai"),
        ))
