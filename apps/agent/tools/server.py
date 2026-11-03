"""Anthropic server tools the orchestrator may use (AGENT_WEB_TOOLS_ENABLED).

They run on Anthropic's side; their blocks (server_tool_use, the
*_tool_result blocks and the code execution blocks of dynamic filtering)
are stored in wire as received. Never set eager_input_streaming on them.
"""
from __future__ import annotations

WEB_SEARCH_TYPE = "web_search_20260209"
WEB_FETCH_TYPE = "web_fetch_20260209"


def server_tool_definitions(*, web_search_max_uses: int, web_fetch_max_uses: int) -> list[dict]:
    return [
        {"type": WEB_SEARCH_TYPE, "name": "web_search", "max_uses": int(web_search_max_uses)},
        {"type": WEB_FETCH_TYPE, "name": "web_fetch", "max_uses": int(web_fetch_max_uses)},
    ]
