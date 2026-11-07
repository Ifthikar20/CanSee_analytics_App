"""Request validation of the Context API (design/p2_contract.md 11b).

Responses are plain dicts built by services.context. The limits a field
can check on its own are here; the rest (a page off the site, a fact that
is not approved, the entries left in a section) are the service's.
"""
from __future__ import annotations

from rest_framework import serializers

from apps.agent.constants import (
    CONTEXT_DOCUMENT_TITLE_MAX_LENGTH,
    CONTEXT_TITLE_MAX_LENGTH,
    ReferenceKind,
)
from apps.agent.services.context import (
    BODY_MAX_LENGTH,
    DOCUMENT_MAX_CHARS,
    MAX_REFERENCES_PER_ENTRY,
    QUOTE_MAX_LENGTH,
    SECTION_KEYS,
)

MAX_REVIEW_IDS = 300


class RefInputSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=ReferenceKind.values)
    url = serializers.CharField(max_length=2000, required=False, allow_blank=True, default="")
    fact_id = serializers.UUIDField(required=False, allow_null=True, default=None)
    document_id = serializers.UUIDField(required=False, allow_null=True, default=None)
    competitor_domain = serializers.CharField(max_length=253, required=False, allow_blank=True,
                                              default="")
    quote = serializers.CharField(max_length=QUOTE_MAX_LENGTH, required=False, allow_blank=True,
                                  default="")

    def validate(self, attrs):
        needs = {
            ReferenceKind.PAGE: ("url", "Enter the page's address."),
            ReferenceKind.FACT: ("fact_id", "Choose a brand fact."),
            ReferenceKind.DOCUMENT: ("document_id", "Choose a document."),
            ReferenceKind.COMPETITOR: ("competitor_domain", "Choose a competitor."),
        }
        field, message = needs[attrs["kind"]]
        if not attrs.get(field):
            raise serializers.ValidationError({field: [message]})
        return attrs


class EntryCreateSerializer(serializers.Serializer):
    section = serializers.ChoiceField(choices=SECTION_KEYS)
    title = serializers.CharField(max_length=CONTEXT_TITLE_MAX_LENGTH, required=False,
                                  allow_blank=True, default="")
    body = serializers.CharField(max_length=BODY_MAX_LENGTH, required=False, allow_blank=True,
                                 default="")
    references = RefInputSerializer(many=True, required=False, default=list,
                                    max_length=MAX_REFERENCES_PER_ENTRY)

    def validate(self, attrs):
        if not attrs.get("title") and not attrs.get("body"):
            raise serializers.ValidationError({"title": ["Write a title or a text."]})
        return attrs


class EntryUpdateSerializer(serializers.Serializer):
    revision = serializers.IntegerField(min_value=0)
    title = serializers.CharField(max_length=CONTEXT_TITLE_MAX_LENGTH, required=False,
                                  allow_blank=True)
    body = serializers.CharField(max_length=BODY_MAX_LENGTH, required=False, allow_blank=True)
    section = serializers.ChoiceField(choices=SECTION_KEYS, required=False)
    position = serializers.IntegerField(min_value=0, required=False)


class RevisionSerializer(serializers.Serializer):
    revision = serializers.IntegerField(min_value=0)


class ReviewEntriesSerializer(serializers.Serializer):
    accept = serializers.ListField(child=serializers.UUIDField(), required=False, default=list,
                                   max_length=MAX_REVIEW_IDS)
    dismiss = serializers.ListField(child=serializers.UUIDField(), required=False, default=list,
                                    max_length=MAX_REVIEW_IDS)

    def validate(self, attrs):
        if not attrs["accept"] and not attrs["dismiss"]:
            raise serializers.ValidationError("Choose suggestions to accept or dismiss.")
        return attrs


class DocumentCreateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=CONTEXT_DOCUMENT_TITLE_MAX_LENGTH)
    text = serializers.CharField(max_length=DOCUMENT_MAX_CHARS)


class DocumentUpdateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=CONTEXT_DOCUMENT_TITLE_MAX_LENGTH, required=False)
    text = serializers.CharField(max_length=DOCUMENT_MAX_CHARS, required=False)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("Send a title or a text.")
        return attrs
