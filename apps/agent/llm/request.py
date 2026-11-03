"""Request bodies for client.beta.messages.stream / messages.create.

The orchestrator replays a chat's pinned snapshot (system, tools, model,
effort, thinking) byte for byte, with a 1h breakpoint on the system
block and top-level automatic caching for the growing history. Sub-agents
and titles are separate prefixes with their own system prompts.

One exception: the server web tools follow the AGENT_WEB_TOOLS_ENABLED
and AGENT_WEB_*_MAX_USES settings of now, also in chats started before
an operator changed them. Such a request no longer replays the tools
its chat's thinking blocks were produced with, so it asks the API to
drop those blocks (thinking.block_binding) instead of rejecting it.
"""
from __future__ import annotations

import json

from apps.agent.conf import agent_conf
from apps.agent.prompts import title as title_prompt

FALLBACK_BETA = "server-side-fallback-2026-07-01"
THINKING_BINDING_BETA = "thinking-binding-controls-2026-08-01"
THINKING = {"type": "adaptive", "display": "summarized"}
MAX_OUTPUT_TOKENS = 64000
_WEB_TOOL_TYPES = ("web_search_", "web_fetch_")


def _cache(ttl: str) -> dict:
    return {"type": "ephemeral", "ttl": ttl}


def _with_fallbacks(request: dict) -> dict:
    if agent_conf().fallbacks_enabled:
        request["betas"] = [FALLBACK_BETA]
        request["fallbacks"] = "default"
    return request


def _web_tools_as_configured(request: dict) -> dict:
    """Withhold the server web tools while they are turned off and lower a
    max_uses above the current setting (see the module docstring)."""
    conf = agent_conf()
    limits = {"web_search": conf.web_search_max_uses, "web_fetch": conf.web_fetch_max_uses}
    tools, changed = [], False
    for tool in request["tools"]:
        name = tool.get("name")
        if str(tool.get("type") or "").startswith(_WEB_TOOL_TYPES) and name in limits:
            if not conf.web_tools_enabled:
                changed = True
                continue
            if tool.get("max_uses") is None or int(tool["max_uses"]) > limits[name]:
                tool = {**tool, "max_uses": limits[name]}
                changed = True
        tools.append(tool)
    if changed:
        request["tools"] = tools
        request["thinking"] = {**request["thinking"],
                               "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
        request["betas"] = [*request.get("betas", []), THINKING_BINDING_BETA]
    return request


def build_turn_request(snapshot, messages: list[dict], *, max_tokens: int | None = None) -> dict:
    """The orchestrator request for one step of a turn."""
    config = snapshot.config or {}
    ttl = config.get("cache_ttl") or agent_conf().cache_ttl
    return _web_tools_as_configured(_with_fallbacks({
        "model": config["model"],
        "max_tokens": int(max_tokens or config["max_tokens"]),
        "system": [{"type": "text", "text": snapshot.system_text, "cache_control": _cache(ttl)}],
        "cache_control": _cache(ttl),
        "tools": json.loads(snapshot.tools_json),
        "thinking": config.get("thinking") or THINKING,
        "output_config": {"effort": config["effort"]},
        "messages": messages,
    }))


def build_subagent_request(spec, content: list[dict], *, max_tokens: int | None = None) -> dict:
    """One writing agent's request: no tools, its own cached system prompt.
    ``max_tokens`` defaults to the agent's own AGENT_*_MAX_TOKENS."""
    conf = agent_conf()
    output_config: dict = {"effort": conf.subagent_effort}
    if spec.output_schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": spec.output_schema}
    return _with_fallbacks({
        "model": conf.model,
        "max_tokens": int(max_tokens or spec.max_tokens(conf)),
        "system": [{"type": "text", "text": spec.system, "cache_control": _cache(conf.cache_ttl)}],
        "thinking": THINKING,
        "output_config": output_config,
        "messages": [{"role": "user", "content": content}],
    })


def build_title_request(text: str) -> dict:
    """A non-streaming request for a chat title (structured JSON output)."""
    conf = agent_conf()
    return {
        "model": conf.model,
        "max_tokens": title_prompt.MAX_TOKENS,
        "system": title_prompt.SYSTEM_PROMPT,
        "output_config": {
            "effort": conf.title_effort,
            "format": {"type": "json_schema", "schema": title_prompt.SCHEMA},
        },
        "messages": [{"role": "user", "content": [{"type": "text", "text": text[:4000]}]}],
    }
