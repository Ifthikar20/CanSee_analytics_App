"""RequestIDMiddleware: id sanitisation and the per-request trace log."""

import logging
from contextlib import contextmanager

from django.http import HttpResponse
from django.test import RequestFactory

from core.middleware.request_id import RequestIDMiddleware

factory = RequestFactory()


def _run(path="/api/v1/websites/", **headers):
    middleware = RequestIDMiddleware(lambda request: HttpResponse(status=200))
    request = factory.get(path, **headers)
    return request, middleware(request)


@contextmanager
def _captured():
    # The "core" logger is propagate=False, so pytest's caplog (a root
    # handler) never sees these records — attach a handler directly.
    class Capture(logging.Handler):
        def __init__(self):
            super().__init__(level=logging.INFO)
            self.records = []

        def emit(self, record):
            self.records.append(record)

    handler = Capture()
    logger = logging.getLogger("core.middleware.request_id")
    logger.addHandler(handler)
    try:
        yield handler.records
    finally:
        logger.removeHandler(handler)


class TestRequestID:
    def test_client_supplied_request_id_is_echoed(self):
        _, response = _run(HTTP_X_REQUEST_ID="abc-123")
        assert response["X-Request-ID"] == "abc-123"

    def test_request_id_is_sanitised(self):
        _, response = _run(HTTP_X_REQUEST_ID="abc<script>%0d%0a!")
        assert response["X-Request-ID"] == "abcscript0d0a"

    def test_generated_when_missing_or_fully_invalid(self):
        _, response = _run(HTTP_X_REQUEST_ID="<<<>>>")
        assert len(response["X-Request-ID"]) == 36  # a fresh uuid4


class TestTraceLog:
    def test_api_request_with_client_id_logs_one_line(self):
        with _captured() as records:
            request, _ = _run(HTTP_X_CLIENT_ID="web-abc123")
        assert request.client_id == "web-abc123"
        assert len(records) == 1
        assert records[0].client_id == "web-abc123"
        assert records[0].status_code == 200
        assert records[0].duration_ms >= 0

    def test_client_id_is_sanitised_in_log(self):
        with _captured() as records:
            _run(HTTP_X_CLIENT_ID='web-x"\n{evil}')
        assert records[0].client_id == "web-xevil"

    def test_no_client_id_no_trace_line(self):
        with _captured() as records:
            _run()
        assert not records

    def test_non_api_path_not_traced(self):
        with _captured() as records:
            _run(path="/health/", HTTP_X_CLIENT_ID="web-abc123")
        assert not records
