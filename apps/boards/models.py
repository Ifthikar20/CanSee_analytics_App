"""Saved analytics boards (board contract 6).

One row per user and project: the widgets that user put on the
project's board, in order, and a version every save bumps, so a window
holding an older board gets a conflict instead of overwriting a newer one.
"""

import uuid

from django.conf import settings
from django.db import models

from core.mixins.timestamp_mixin import TimestampMixin


class BoardLayout(TimestampMixin):
    """One user's board for one project; no one reads another user's.

    ``widgets`` is ``[{key, size}]`` as the last save sent it (checked by
    layout.validate_widgets); readers go through layout.read_widgets. Reset
    rewrites the row and bumps ``version`` instead of deleting it: a new
    row would restart at version 1, which a stale window could match.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="board_layouts",
        on_delete=models.CASCADE,
    )
    website = models.ForeignKey(
        "websites.Website",
        related_name="board_layouts",
        on_delete=models.CASCADE,
    )
    widgets = models.JSONField(default=list)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        db_table = "boards_boardlayout"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "website"], name="uniq_board_layout_user_website",
            ),
        ]

    def __str__(self):
        return f"BoardLayout({self.user_id}, {self.website_id}, v{self.version})"
