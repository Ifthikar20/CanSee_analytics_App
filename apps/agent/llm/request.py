"""Request bodies for client.beta.messages.stream / messages.create.

The orchestrator replays a chat's pinned snapshot (system, tools, model,
effort, thinking) byte for byte, with a 1h breakpoint on the system
block and top-level automatic caching for the growing history. Sub-agents
and titles are separate prefixes with their own system prompts.
"""
from __future__ import annotations

import json

from apps.agent.conf import agent_conf
from apps.agent.prompts import title as title_prompt

FALLBACK_BETA = "server-side-fallback-2026-07-01"
THINKING = {"type": "adaptive", "display": "summarized"}
MAX_OUTPUT_TOKENS = 64000


def _cache(ttl: str) -> dict:
    return {"type": "ephemeral", "ttl": ttl}


def _with_fallbacks(request: dict) -> dict:
    if agent_conf().fallbacks_enabled:
        request["betas"] = [FALLBACK_BETA]
        request["fallbacks"] = "default"
    return request


def build_turn_request(snapshot, messages: list[dict], *, max_tokens: int | None = None) -> dict:
    """The orchestrator request for one step of a turn."""
    config = snapshot.config or {}
    ttl = config.get("cache_ttl") or agent_conf().cache_ttl
    return _with_fallbacks({
        "model": config["model"],
        "max_tokens": int(max_tokens or config["max_tokens"]),
        "system": [{"type": "text", "text": snapshot.system_text, "cache_control": _cache(ttl)}],
        "cache_control": _cache(ttl),
        "tools": json.loads(snapshot.tools_json),
        "thinking": config.get("thinking") or THINKING,
        "output_config": {"effort": config["effort"]},
        "messages": messages,
    })


def build_subagent_request(spec, content: list[dict]) -> dict:
    """One writing agent's request: no tools, its own cached system prompt."""
    conf = agent_conf()
    output_config: dict = {"effort": conf.subagent_effort}
    if spec.output_schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": spec.output_schema}
    return _with_fallbacks({
        "model": conf.model,
        "max_tokens": int(spec.max_tokens(conf)),
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
