"""Content-addressed prompt snapshots: what a chat replays against.

A snapshot is built from code constants (the orchestrator prompt, the
tool definitions and the AGENT_* request config) into canonical JSON;
its sha256 is the primary key. Every entry agent shares the orchestrator
prompt, so every chat on the same code and settings sends the same
system and tools bytes (one cache prefix).
"""
from __future__ import annotations

import hashlib
import json

from django.db import IntegrityError

from apps.agent.conf import agent_conf
from apps.agent.constants import DEFAULT_ENTRY_AGENT
from apps.agent.llm.request import THINKING
from apps.agent.prompts import orchestrator
from apps.agent.tools.registry import tool_definitions


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def snapshot_payload(agent_key: str = DEFAULT_ENTRY_AGENT) -> dict:
    conf = agent_conf()
    tools = tool_definitions(
        web_tools=conf.web_tools_enabled,
        web_search_max_uses=conf.web_search_max_uses,
        web_fetch_max_uses=conf.web_fetch_max_uses,
    )
    return {
        "agent_key": agent_key,
        "version": orchestrator.VERSION,
        "system": orchestrator.SYSTEM_PROMPT,
        "tools": tools,
        "config": {
            "model": conf.model,
            "effort": conf.effort,
            "thinking": dict(THINKING),
            "max_tokens": conf.max_tokens,
            "cache_ttl": conf.cache_ttl,
        },
    }


def snapshot_id(payload: dict) -> str:
    return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()


def get_or_create_snapshot(agent_key: str = DEFAULT_ENTRY_AGENT):
    """The snapshot a new chat with this entry agent pins."""
    from apps.agent.models import AgentPromptSnapshot

    payload = snapshot_payload(agent_key)
    sid = snapshot_id(payload)
    defaults = {
        "agent_key": agent_key,
        "version": payload["version"],
        "system_text": payload["system"],
        "tools_json": canonical(payload["tools"]),
        "config": payload["config"],
    }
    try:
        snapshot, _ = AgentPromptSnapshot.objects.get_or_create(id=sid, defaults=defaults)
    except IntegrityError:
        snapshot = AgentPromptSnapshot.objects.get(id=sid)
    return snapshot
