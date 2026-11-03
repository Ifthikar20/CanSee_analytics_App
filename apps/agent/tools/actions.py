"""Action tools: writing agents, charts and links for the browser pane."""
from __future__ import annotations

import math

from apps.agent.constants import PartType
from apps.agent.tools.context import ToolContext, ToolError, ToolResult
from apps.agent.tools.shaping import clip, host_of

MAX_LABELS = 12
MAX_DATASETS = 3
CHART_TYPES = ("bar", "line", "doughnut")


def run_agent(ctx: ToolContext, args) -> ToolResult:
    from apps.agent.subagents.base import run_subagent

    return run_subagent(ctx, args)


def _number(value):
    """A finite number, or None for a gap (the old UI's coercion)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return value if math.isfinite(value) else None
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() else number


def parse_chart_spec(spec) -> dict | None:
    """The chart rules of the old UI's answerParts.parseChartSpec: bar,
    line or doughnut (else bar); at most 12 labels and 3 datasets; values
    numeric or null; a dataset without any number is dropped; title at
    most 120 characters and unit at most 8. None when nothing is left."""
    data = spec.model_dump() if hasattr(spec, "model_dump") else dict(spec or {})
    chart_type = data.get("type") if data.get("type") in CHART_TYPES else "bar"
    labels = [str(label) for label in (data.get("labels") or [])[:MAX_LABELS]]
    if not labels:
        return None
    datasets = []
    for ds in (data.get("datasets") or [])[:MAX_DATASETS]:
        values = [_number(v) for v in (ds.get("data") or [])[: len(labels)]]
        if any(v is not None for v in values):
            datasets.append({"label": str(ds.get("label") or ""), "data": values})
    if not datasets:
        return None
    title = data.get("title")
    unit = data.get("unit")
    return {
        "type": chart_type,
        "title": title[:120] if isinstance(title, str) else "",
        "labels": labels,
        "datasets": datasets,
        "unit": unit[:8] if isinstance(unit, str) else "",
    }


def show_chart(ctx: ToolContext, args) -> ToolResult:
    spec = parse_chart_spec(args.spec)
    if spec is None:
        raise ToolError(
            "The chart needs at least one label and one dataset with a number.",
            summary="Invalid chart",
        )
    ctx.emitter.add_part(PartType.CHART, request=ctx.request, spec=spec)
    return ToolResult.ok({"shown": True, "type": spec["type"], "labels": len(spec["labels"]),
                          "datasets": len(spec["datasets"])},
                         summary=spec["title"] or "Chart")


def _known_hosts(ctx: ToolContext) -> set[str]:
    from apps.citations.services.url_normalizer import extract_apex_domain

    hosts = {extract_apex_domain(host_of(ctx.website.url))}
    for c in ctx.website.competitors or []:
        if isinstance(c, dict) and c.get("domain"):
            hosts.add(extract_apex_domain(str(c["domain"]).strip().lower()))
    hosts.discard("")
    return hosts


def open_in_browser(ctx: ToolContext, args) -> ToolResult:
    from apps.citations.services.url_normalizer import extract_apex_domain

    url = args.url
    if not url.lower().startswith(("http://", "https://")) or not host_of(url):
        raise ToolError("open_in_browser needs an http or https URL.", summary="Invalid URL")
    seen_in_chat = url in (ctx.history_text or "")
    own_or_competitor = extract_apex_domain(host_of(url)) in _known_hosts(ctx)
    requires_confirmation = not (seen_in_chat or own_or_competitor)
    title = clip(args.title or host_of(url), 120)
    ctx.emitter.add_part(PartType.BROWSER_OPEN, request=ctx.request, url=url, title=title,
                         requires_confirmation=requires_confirmation)
    return ToolResult.ok(
        {"url": url, "requires_confirmation": requires_confirmation,
         "note": ("The user must click the link to open it." if requires_confirmation
                  else "The link is shown in the reply; the user opens it from there.")},
        summary=title,
    )
