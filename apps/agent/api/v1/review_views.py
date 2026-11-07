"""Approvals API (design/p2_contract.md 13c): an artifact's review, its
comments, the Reviews page's boxes and the project's team. Reads are open
to viewers; actions, comments and the team (it carries teammates' emails)
need write access."""
from __future__ import annotations

from rest_framework import serializers
from rest_framework.response import Response

from apps.agent.api.v1.serializers import ArtifactSummarySerializer
from apps.agent.api.v1.views import AgentAPIView, InvalidRequest, _validated
from apps.agent.services import artifacts, reviews


class ReviewActionSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=reviews.ACTIONS)
    revision = serializers.IntegerField(min_value=0)
    reviewer_id = serializers.UUIDField(required=False, allow_null=True, default=None)
    note = serializers.CharField(max_length=reviews.NOTE_MAX_LENGTH, required=False,
                                 allow_blank=True, default="")


class ReviewCommentSerializer(serializers.Serializer):
    body = serializers.CharField(max_length=reviews.NOTE_MAX_LENGTH)


class ArtifactReviewView(AgentAPIView):
    def get(self, request, website_id, artifact_id):
        artifact = artifacts.get_artifact(self.website(), artifact_id)
        return Response(reviews.review_payload(artifact, request.user))

    def post(self, request, website_id, artifact_id):
        artifact = artifacts.get_artifact(self.website(write=True), artifact_id)
        data = _validated(ReviewActionSerializer, request.data)
        reviews.act(artifact, request.user, action=data["action"], revision=data["revision"],
                    reviewer_id=data["reviewer_id"], note=data["note"])
        return Response(reviews.review_payload(artifact, request.user))


class ArtifactReviewCommentsView(AgentAPIView):
    def post(self, request, website_id, artifact_id):
        artifact = artifacts.get_artifact(self.website(write=True), artifact_id)
        data = _validated(ReviewCommentSerializer, request.data)
        event = reviews.comment(artifact, request.user, data["body"])
        return Response(reviews.event_payload(event), status=201)


class ReviewListView(AgentAPIView):
    def get(self, request, website_id):
        website = self.website()
        box = request.query_params.get("box") or "waiting"
        if box not in reviews.BOXES:
            raise InvalidRequest({"box": ["Choose waiting, mine or approved."]})
        items = reviews.list_reviews(request.user, website, box)
        return Response(list(ArtifactSummarySerializer(items, many=True,
                                                       context={"user": request.user}).data))


class TeamView(AgentAPIView):
    def get(self, request, website_id):
        website = self.website(write=True)
        return Response(reviews.team_payload(website, request.user))
