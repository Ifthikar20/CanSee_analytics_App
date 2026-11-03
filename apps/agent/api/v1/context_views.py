"""The Context API (design/p2_contract.md 11b): sections, entries, their
sources and the documents people add. Viewers read; members and above
write. Build from my site is the ``context_build`` kind of the insights
runs API (services.context_build)."""
from __future__ import annotations

from rest_framework.response import Response

from apps.agent.api.v1.context_serializers import (
    DocumentCreateSerializer,
    DocumentUpdateSerializer,
    EntryCreateSerializer,
    EntryUpdateSerializer,
    RefInputSerializer,
    ReviewEntriesSerializer,
    RevisionSerializer,
)
from apps.agent.api.v1.views import AgentAPIView, _validated
from apps.agent.services import context


def _entry(website, entry) -> dict:
    return context.entry_payload(context.load_entry(entry),
                                 context.tracked_competitors(website))


class ContextView(AgentAPIView):
    def get(self, request, website_id):
        return Response(context.overview(self.website(), request.user))


class ContextEntriesView(AgentAPIView):
    def post(self, request, website_id):
        website = self.website(write=True)
        data = _validated(EntryCreateSerializer, request.data)
        entry = context.create_entry(
            website, request.user, section=data["section"], title=data["title"],
            body=data["body"], references=[dict(r) for r in data["references"]],
        )
        return Response(_entry(website, entry), status=201)


class ContextEntryView(AgentAPIView):
    def patch(self, request, website_id, entry_id):
        website = self.website(write=True)
        entry = context.get_entry(website, entry_id)
        data = dict(_validated(EntryUpdateSerializer, request.data))
        revision = data.pop("revision")
        entry = context.update_entry(entry, request.user, revision=revision, **data)
        return Response(_entry(website, entry))

    def delete(self, request, website_id, entry_id):
        website = self.website(write=True)
        entry = context.get_entry(website, entry_id)
        source = request.query_params if "revision" in request.query_params else request.data
        data = _validated(RevisionSerializer, {"revision": source.get("revision")})
        context.delete_entry(entry, revision=data["revision"])
        return Response(status=204)


class ContextReviewView(AgentAPIView):
    def post(self, request, website_id):
        website = self.website(write=True)
        data = _validated(ReviewEntriesSerializer, request.data)
        return Response(context.review_entries(website, request.user, accept=data["accept"],
                                               dismiss=data["dismiss"]))


class ContextReferencesView(AgentAPIView):
    def post(self, request, website_id, entry_id):
        website = self.website(write=True)
        entry = context.get_entry(website, entry_id)
        data = _validated(RefInputSerializer, request.data)
        reference = context.add_reference(entry, request.user, dict(data))
        return Response(context.reference_payload(reference, context.tracked_competitors(website)),
                        status=201)


class ContextReferenceView(AgentAPIView):
    def delete(self, request, website_id, reference_id):
        website = self.website(write=True)
        context.delete_reference(context.get_reference(website, reference_id))
        return Response(status=204)


class ContextDocumentsView(AgentAPIView):
    def get(self, request, website_id):
        return Response(context.list_documents(self.website()))

    def post(self, request, website_id):
        website = self.website(write=True)
        data = _validated(DocumentCreateSerializer, request.data)
        document = context.create_document(website, request.user, title=data["title"],
                                           text=data["text"])
        return Response(context.document_payload(context.get_document(website, document.id)),
                        status=201)


class ContextDocumentView(AgentAPIView):
    def get(self, request, website_id, document_id):
        document = context.get_document(self.website(), document_id)
        return Response(context.document_payload(document, text=True))

    def patch(self, request, website_id, document_id):
        website = self.website(write=True)
        document = context.get_document(website, document_id)
        data = _validated(DocumentUpdateSerializer, request.data)
        context.update_document(document, request.user, title=data.get("title"),
                                text=data.get("text"))
        return Response(context.document_payload(context.get_document(website, document_id),
                                                 text=True))

    def delete(self, request, website_id, document_id):
        website = self.website(write=True)
        context.delete_document(context.get_document(website, document_id))
        return Response(status=204)
