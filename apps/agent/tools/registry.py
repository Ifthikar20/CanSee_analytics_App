"""The orchestrator's tools: definitions for the snapshot, and execution.

Client tools validate their input with pydantic before running; invalid
input, an unknown tool and a handler failure all come back to the model
as ``is_error`` results instead of failing the run.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import ValidationError

from apps.agent.conf import agent_conf
from apps.agent.llm.gate import CancelRequested, GateStop
from apps.agent.tools import actions, data, research, swarm
from apps.agent.tools.context import ToolContext, ToolError, ToolResult
from apps.agent.tools.schemas import DEFINITIONS, MODELS
from apps.agent.tools.server import server_tool_definitions
from apps.agent.tools.shaping import compact

logger = logging.getLogger("apps")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    handler: Callable[[ToolContext, object], ToolResult]
    # Step line for tools shown as a generic ``tool`` part.
    label: str
    # The tool adds its own parts (serp, page_inspect, engine_check, ...).
    own_parts: bool = False


TOOLS: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        ToolSpec("get_project_overview", data.get_project_overview, "Project overview"),
        ToolSpec("list_tracked_prompts", data.list_tracked_prompts, "Tracked prompts"),
        ToolSpec("get_prompt_gap", data.get_prompt_gap, "Google vs AI gap"),
        ToolSpec("get_ai_answers", data.get_ai_answers, "AI engine answers"),
        ToolSpec("get_cited_sources", data.get_cited_sources, "Cited sources"),
        ToolSpec("get_brand_context", data.get_brand_context, "Brand context"),
        ToolSpec("list_artifacts", data.list_artifacts, "Artifacts"),
        ToolSpec("track_prompts", research.track_prompts, "Track prompts"),
        ToolSpec("google_search", research.google_search, "Google search", own_parts=True),
        ToolSpec("inspect_page", research.inspect_page, "Inspect page", own_parts=True),
        ToolSpec("check_ai_engines", research.check_ai_engines, "Ask AI engines", own_parts=True),
        ToolSpec("run_agent", actions.run_agent, "Writing agent", own_parts=True),
        ToolSpec("show_chart", actions.show_chart, "Chart", own_parts=True),
        ToolSpec("open_in_browser", actions.open_in_browser, "Open link", own_parts=True),
        # The swarm and swarm_table parts; the translator adds the planning
        # part when the call starts streaming.
        ToolSpec("research_swarm", swarm.research_swarm, "Research agents", own_parts=True),
    )
}

SERVER_TOOL_NAMES = ("web_search", "web_fetch")
# Offered only while AGENT_SWARM_ENABLED is on.
SWARM_TOOL_NAME = "research_swarm"


def client_tool_definitions() -> list[dict]:
    """The client tools a new snapshot offers. research_swarm is left out
    while the research agents are turned off (chats pinned to a snapshot
    that has it get a swarm_disabled result instead)."""
    swarm_enabled = agent_conf().swarm_enabled
    return [
        {
            "name": name,
            "description": DEFINITIONS[name]["description"],
            "input_schema": DEFINITIONS[name]["input_schema"],
            "eager_input_streaming": True,
        }
        for name in TOOLS
        if swarm_enabled or name != SWARM_TOOL_NAME
    ]


def tool_definitions(*, web_tools: bool, web_search_max_uses: int,
                     web_fetch_max_uses: int) -> list[dict]:
    """Every tool of the orchestrator, sorted by name (a stable prefix)."""
    tools = client_tool_definitions()
    if web_tools:
        tools += server_tool_definitions(
            web_search_max_uses=web_search_max_uses, web_fetch_max_uses=web_fetch_max_uses,
        )
    return sorted(tools, key=lambda t: t["name"])


def label_for(name: str) -> str:
    spec = TOOLS.get(name)
    return spec.label if spec else name


def owns_parts(name: str) -> bool:
    spec = TOOLS.get(name)
    return bool(spec and spec.own_parts)


def _validation_errors(exc: ValidationError) -> list[str]:
    out = []
    for err in exc.errors()[:5]:
        where = ".".join(str(p) for p in err.get("loc") or ()) or "input"
        out.append(f"{where}: {err.get('msg')}")
    return out


def execute(ctx: ToolContext, name: str, raw_input) -> ToolResult:
    """Run one tool call and return its tool_result content."""
    spec = TOOLS.get(name)
    if spec is None:
        return ToolResult.error(f"Unknown tool: {name}.", summary="Unknown tool")
    model = MODELS[name]
    try:
        if not isinstance(raw_input, dict):
            raise TypeError("not an object")
        args = model.model_validate(raw_input)
    except (ValidationError, TypeError) as exc:
        detail = {"INVALID_JSON": json.dumps(raw_input, ensure_ascii=False, default=str)}
        if isinstance(exc, ValidationError):
            detail["errors"] = _validation_errors(exc)
        return ToolResult(content=compact(detail), is_error=True, summary="Invalid input")
    try:
        result = spec.handler(ctx, args)
    except ToolError as exc:
        return ToolResult(content=exc.message, is_error=True, summary=exc.summary)
    except (CancelRequested, GateStop):
        raise
    except Exception:
        logger.exception("agent tool %s failed (run %s)", name, ctx.run.id)
        return ToolResult.error("The tool failed with an internal error.", summary="Failed")
    if isinstance(result, ToolResult):
        return result
    return ToolResult.ok(result)
