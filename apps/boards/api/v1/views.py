"""Board layout API (board contract 6), under /api/v1/boards/:

GET     websites/<wid>/layout/   the user's saved board, or the default
PUT     websites/<wid>/layout/   save {version, widgets}; 409 when the version is stale
DELETE  websites/<wid>/layout/   Reset board: the default layout, one version up

A layout belongs to one user: viewers arrange their own board too, a
caller who cannot reach the project gets 404 and a signed-out caller 401.
A save or reset locks the user's row, so versions bump one at a time; two
first saves racing on the unique constraint answer 409 like any other
stale save.
"""

from django.db import IntegrityError, transaction
from rest_framework.response import Response

from apps.boards import layout
from apps.boards.models import BoardLayout
from core.exceptions import CanseeException
from core.views.base import TenantScopedAPIView


class LayoutConflict(CanseeException):
    """409 with the stored layout, which the client adopts."""

    def __init__(self, current: BoardLayout | None):
        super().__init__(
            "Your board changed in another window.",
            code="layout_conflict",
            status_code=409,
            details={"layout": layout.payload(current)},
        )


def _row(user, website, *, lock: bool = False) -> BoardLayout | None:
    rows = BoardLayout.objects.filter(user=user, website=website)
    return (rows.select_for_update() if lock else rows).first()


class BoardLayoutView(TenantScopedAPIView):
    def get_website(self, website_id=None):
        """Viewers pass: the layout is the caller's own, not project data."""
        from apps.websites.services.website_service import WebsiteService

        if website_id is None:
            website_id = self.kwargs.get(self.website_url_kwarg)
        return WebsiteService.get_for_user(
            user=self.request.user, website_id=website_id, min_role="viewer",
        )

    def get(self, request, website_id):
        website = self.get_website(website_id)
        return Response(layout.payload(_row(request.user, website)))

    def put(self, request, website_id):
        website = self.get_website(website_id)
        body = request.data if isinstance(request.data, dict) else {}
        version = layout.validate_version(body.get("version"))
        widgets = layout.validate_widgets(body.get("widgets"))
        with transaction.atomic():
            row = _row(request.user, website, lock=True)
            if version != (row.version if row else 0):
                raise LayoutConflict(row)
            if row is None:
                try:
                    with transaction.atomic():
                        row = BoardLayout.objects.create(
                            user=request.user, website=website, widgets=widgets,
                        )
                except IntegrityError:
                    # Another window's first save committed in between.
                    raise LayoutConflict(_row(request.user, website)) from None
            else:
                row.widgets = widgets
                row.version += 1
                row.save(update_fields=["widgets", "version", "updated_at"])
        return Response(layout.payload(row))

    def delete(self, request, website_id):
        website = self.get_website(website_id)
        with transaction.atomic():
            row = _row(request.user, website, lock=True)
            # Nothing saved: the default is already what GET answers.
            if row is not None:
                row.widgets = layout.default_widgets()
                row.version += 1
                row.save(update_fields=["widgets", "version", "updated_at"])
        return Response(layout.payload(row))
