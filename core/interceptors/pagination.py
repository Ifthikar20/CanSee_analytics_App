from rest_framework.pagination import CursorPagination, PageNumberPagination


def _mark_page(response):
    # EnvelopeRenderer unpacks a response marked as a page into data/meta.
    response.paginated = True
    return response


class StandardPagination(PageNumberPagination):
    """Standard offset-based pagination for most endpoints."""
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100
    page_query_param = "page"

    def get_paginated_response(self, data):
        return _mark_page(super().get_paginated_response(data))


class CursorBasedPagination(CursorPagination):
    """Cursor-based pagination for high-volume feeds (events, notifications)."""
    page_size = 50
    ordering = "-created_at"
    cursor_query_param = "cursor"

    def get_paginated_response(self, data):
        return _mark_page(super().get_paginated_response(data))
