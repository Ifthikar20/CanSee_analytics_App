"""The usage dashboard (design/p2_contract.md 14b). Signed in only; not
gated by AGENT_ENABLED, since it also reports spend outside the agent."""
from __future__ import annotations

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.agent.services import command_center


class CommandCenterView(APIView):
    permission_classes = [IsAuthenticated]
    schema = None

    def get(self, request):
        params = request.query_params
        return Response(command_center.build(
            request.user, scope=params.get("scope"), period=params.get("period"),
            interval=params.get("interval"), website_id=params.get("website_id"),
        ))
