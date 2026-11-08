from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response


class PageResponse(Response):
    """A response whose body is one page of a list.

    ``{"results": [...], ...}`` built by hand (rather than by a DRF
    paginator) must use this class so EnvelopeRenderer unpacks it into
    ``data`` and ``meta``. A plain ``Response`` whose body merely has a
    ``results`` key (an audit with its result rows, a search payload) is
    left as it is.
    """

    paginated = True


def is_page(data, response) -> bool:
    """True when ``data`` is one page of a paginated list.

    An explicit ``paginated`` attribute on the response wins. Otherwise the
    body must have the shape every DRF paginator emits: a ``results`` list
    beside ``next`` and ``previous`` links. A ``results`` key on its own is
    not enough, because detail payloads use that name for nested rows.
    """
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        return False
    flag = getattr(response, "paginated", None)
    if flag is not None:
        return bool(flag)
    return "next" in data and "previous" in data


class EnvelopeRenderer(JSONRenderer):
    """
    Wraps all successful responses in:
    {
        "success": true,
        "data": { ... },
        "meta": { "count": 100, "next": "...", "previous": "..." }  // If paginated
    }
    """

    def render(self, data, accepted_media_type=None, renderer_context=None):
        response = renderer_context.get("response") if renderer_context else None

        # 204 must not carry a body (RFC 9110). Wrapping it in the envelope
        # makes Django emit JSON on a bodiless status, which proxies and
        # browsers reject with a content-length mismatch.
        if response and response.status_code == 204:
            return b""

        if response and response.status_code >= 400:
            # Error responses already formatted by exception handler
            return super().render(data, accepted_media_type, renderer_context)

        if is_page(data, response):
            meta = {
                "count": data.get("count"),
                "next": data.get("next"),
                "previous": data.get("previous"),
            }
            # Views may attach extra top-level keys to a paginated
            # response (e.g. the rag source list adds ``stats`` so the
            # UI gets badge counts in one round-trip). Carry them into
            # meta instead of silently dropping them.
            for key, value in data.items():
                if key not in ("results", "count", "next", "previous"):
                    meta[key] = value
            envelope = {
                "success": True,
                "data": data["results"],
                "meta": meta,
            }
        else:
            envelope = {"success": True, "data": data}

        return super().render(envelope, accepted_media_type, renderer_context)
