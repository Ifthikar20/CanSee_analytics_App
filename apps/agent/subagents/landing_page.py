"""The landing-page writing agent: one sanitized HTML document."""
from __future__ import annotations

from apps.agent.constants import AgentKey, ArtifactKind
from apps.agent.prompts import landing_page as prompt
from apps.agent.subagents.base import SubagentSpec, Validated
from apps.agent.subagents.html_sanitize import sanitize_html, summarize_html


def validate(raw: str, stop_reason: str | None) -> Validated:
    result = sanitize_html(raw)
    if result.error:
        if stop_reason == "max_tokens":
            raise ValueError("The page was cut off before it was finished.")
        raise ValueError(result.error)
    return Validated(content=result.html, warnings=result.warnings,
                     summary=summarize_html(result.html))


SPEC = SubagentSpec(
    key=AgentKey.LANDING_PAGE.value,
    name="Landing page",
    kind=ArtifactKind.PAGE.value,
    version=prompt.VERSION,
    system=prompt.SYSTEM_PROMPT,
    output_contract=prompt.OUTPUT_CONTRACT,
    max_tokens=lambda conf: conf.landing_max_tokens,
    streams="delta",
    output_schema=None,
    validate=validate,
)
