"""Conversation persistence for Ask Cansee.

The assistant shipped as a side panel whose thread lived only in Pinia, so
reloading the tab lost it. Moving the assistant onto its own page means the
sidebar lists past chats, and a list needs somewhere to read from.

Isolation: a conversation is pinned to BOTH the website and the user who
started it. Every query filters on both, and the website itself is resolved
through the tenant gate first, so a conversation id from another tenant
resolves to nothing rather than to someone else's thread.
"""

import uuid

from django.conf import settings
from django.db import models

from core.mixins.timestamp_mixin import TimestampMixin

# Auto-titles are cut from the first question. Long enough to stay
# recognisable in a narrow sidebar, short enough not to wrap.
TITLE_MAX_CHARS = 60


class AssistantConversation(TimestampMixin):
    """One chat thread between a user and the assistant, for one website."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    website = models.ForeignKey(
        "websites.Website",
        related_name="assistant_conversations",
        on_delete=models.CASCADE,
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="assistant_conversations",
        on_delete=models.CASCADE,
    )
    # Derived from the first question. Blank until that lands, so a
    # conversation opened but never used shows as "New chat".
    title = models.CharField(max_length=120, blank=True)
    # Kept alongside updated_at because a rename should not reorder the
    # sidebar -- only actual conversation activity should.
    last_message_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        db_table = "assistant_conversation"
        ordering = ["-last_message_at", "-created_at"]
        indexes = [
            models.Index(fields=["website", "user", "-last_message_at"]),
        ]

    def __str__(self):
        return f"AssistantConversation({self.title or 'New chat'!r})"

    @staticmethod
    def title_from(question: str) -> str:
        """A sidebar label from the first question.

        Trimmed on a word boundary so a truncated title does not end
        mid-word, which reads like a rendering bug.
        """
        text = " ".join((question or "").split())
        if len(text) <= TITLE_MAX_CHARS:
            return text
        cut = text[:TITLE_MAX_CHARS].rsplit(" ", 1)[0]
        return f"{cut or text[:TITLE_MAX_CHARS]}…"


class AssistantMessage(TimestampMixin):
    """One turn in a conversation."""

    class Role(models.TextChoices):
        USER = "user", "User"
        ASSISTANT = "assistant", "Assistant"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(
        AssistantConversation, related_name="messages", on_delete=models.CASCADE
    )
    role = models.CharField(max_length=16, choices=Role.choices)
    content = models.TextField()
    # Whether real tenant data (figures, rows or knowledge-base passages)
    # was in the prompt for this answer. Persisted so a reloaded thread can
    # still show the grounding badge it showed live.
    grounded = models.BooleanField(default=False)
    # Which model produced this answer. Stored per message, not per
    # conversation: the configured provider can change between turns, and a
    # thread should stay honest about which model said what.
    model = models.CharField(max_length=80, blank=True)

    class Meta:
        db_table = "assistant_message"
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["conversation", "created_at"]),
        ]

    def __str__(self):
        return f"AssistantMessage({self.role}, {self.content[:40]!r})"
