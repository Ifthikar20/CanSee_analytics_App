"""Base APIView classes that encapsulate the project's tenant-scoped contract.

Almost every endpoint in this codebase needs the same three things:

1. The caller must be authenticated.
2. The ``website_id`` URL kwarg must be resolved into a ``Website`` instance,
   and the request user must own / belong to it.
3. List endpoints want :class:`StandardPagination` applied uniformly.

Doing that inline in every view leads to ~100 copy-pasted lines per app and
makes it easy to forget the ownership check on a new endpoint — a security
foot-gun. These bases centralize the contract.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.exceptions import ResourceNotFound
from core.interceptors.pagination import StandardPagination
from core.permissions.rbac import min_role_for_method


class TenantScopedAPIView(APIView):
    """APIView that requires authentication and resolves a website by URL kwarg.

    Subclasses get :meth:`get_website` and :meth:`get_tenant_object` for free
    and never need to set ``permission_classes`` themselves.

    Example::

        class MyView(TenantScopedAPIView):
            def get(self, request, website_id):
                website = self.get_website(website_id)
                return Response({"name": website.name})
    """

    permission_classes = [IsAuthenticated]

    #: Override in subclasses to use a different URL kwarg name (e.g. ``site_id``).
    website_url_kwarg: str = "website_id"

    def get_website(self, website_id=None, *, min_role: str | None = None):
        """Return the ``Website`` for the current request, raising 404/403 if denied.

        Pass an explicit id when the URL kwarg name differs from the convention.
        Without ``min_role`` the request method decides: reads (GET, HEAD,
        OPTIONS) are open to org viewers, everything else needs member.
        Pass ``min_role="admin"`` for destructive project-level actions.
        Lazily imports :class:`WebsiteService` to avoid circular imports during
        Django app loading.
        """
        from apps.websites.services.website_service import WebsiteService

        if website_id is None:
            website_id = self.kwargs.get(self.website_url_kwarg)
        if min_role is None:
            min_role = min_role_for_method(self.request.method)
        return WebsiteService.get_for_user(
            user=self.request.user, website_id=website_id, min_role=min_role,
        )

    def get_tenant_object(self, queryset, **lookup):
        """Return a single object from ``queryset`` matching ``lookup``.

        Raises :class:`ResourceNotFound` (which the global exception handler
        turns into a 404) instead of leaking ``DoesNotExist``. Use this for
        the ``_get_xxx`` helpers that previously lived in every detail view.
        """
        try:
            return queryset.get(**lookup)
        except queryset.model.DoesNotExist as e:
            raise ResourceNotFound(f"{queryset.model.__name__} not found.") from e


class TenantScopedListAPIView(TenantScopedAPIView):
    """Tenant-scoped view with a built-in pagination helper.

    Replaces the 13+ places that manually wire up :class:`StandardPagination`::

        paginator = StandardPagination()
        page = paginator.paginate_queryset(qs, request)
        return paginator.get_paginated_response(serializer_cls(page, many=True).data)

    becomes::

        return self.paginated_response(qs, MySerializer)
    """

    pagination_class = StandardPagination

    def paginated_response(
        self,
        queryset: Iterable[Any],
        serializer_class,
        *,
        serializer_context: dict | None = None,
    ) -> Response:
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(queryset, self.request, view=self)
        ctx = {"request": self.request, **(serializer_context or {})}
        if page is not None:
            data = serializer_class(page, many=True, context=ctx).data
            return paginator.get_paginated_response(data)
        # Pagination disabled (e.g. ?page_size=0) — fall back to flat list.
        data = serializer_class(queryset, many=True, context=ctx).data
        return Response(data)
