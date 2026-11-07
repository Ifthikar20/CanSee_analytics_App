"""
Asserts the RAG ingest serializer rejects unsafe URLs at the API
boundary (defence in depth — the scraper also runs the SSRF guard,
but we want the API to short-circuit before queueing a Celery task).
"""
from unittest.mock import patch

from apps.rag.api.v1.serializers import IngestURLSerializer


def _fake_resolver(*addresses: str):
    import socket as _s

    def fake(host, *args, **kwargs):
        records = []
        for ip in addresses:
            family = _s.AF_INET6 if ":" in ip else _s.AF_INET
            sockaddr = (ip, 0, 0, 0) if family == _s.AF_INET6 else (ip, 0)
            records.append((family, _s.SOCK_STREAM, 0, "", sockaddr))
        return records
    return patch("core.validators.url_safety.socket.getaddrinfo", new=fake)


class TestIngestURLSerializerSSRF:
    def test_accepts_public_url(self):
        with _fake_resolver("93.184.216.34"):
            ser = IngestURLSerializer(data={"url": "https://example.com"})
            assert ser.is_valid(), ser.errors

    def test_rejects_localhost(self):
        ser = IngestURLSerializer(data={"url": "http://localhost/admin"})
        assert ser.is_valid() is False
        assert "url" in ser.errors

    def test_rejects_aws_metadata_endpoint(self):
        with _fake_resolver("169.254.169.254"):
            ser = IngestURLSerializer(data={"url": "http://metadata.example.com/"})
            assert ser.is_valid() is False
            assert "url" in ser.errors

    def test_rejects_rfc1918_resolution(self):
        with _fake_resolver("10.0.0.1"):
            ser = IngestURLSerializer(data={"url": "http://internal.example.com/"})
            assert ser.is_valid() is False

    def test_rejects_ipv6_loopback(self):
        with _fake_resolver("::1"):
            ser = IngestURLSerializer(data={"url": "http://example.com/"})
            assert ser.is_valid() is False

    def test_rejects_one_private_among_many_addresses(self):
        # If DNS returns mixed public + private addresses, refuse — this
        # is the safest call when we can't pin which one ``requests`` will use.
        with _fake_resolver("93.184.216.34", "127.0.0.1"):
            ser = IngestURLSerializer(data={"url": "http://multihomed.example.com/"})
            assert ser.is_valid() is False
