"""The Google Ads search campaign agent: one validated campaign sheet."""
from __future__ import annotations

import json

from apps.agent.constants import AgentKey, ArtifactKind
from apps.agent.prompts import ad_campaign as prompt
from apps.agent.subagents.base import SubagentSpec, Validated
from apps.agent.subagents.sheet_schema import (
    RESPONSE_SCHEMA,
    SheetInvalid,
    summarize_sheet,
    validate_sheet,
)


def validate(raw: str, stop_reason: str | None) -> Validated:
    if stop_reason == "max_tokens":
        raise ValueError("The campaign was cut off before it was finished.")
    try:
        content, warnings = validate_sheet(raw)
    except SheetInvalid as exc:
        raise ValueError(str(exc)) from None
    return Validated(content=json.dumps(content, ensure_ascii=False), warnings=warnings,
                     summary=summarize_sheet(content))


SPEC = SubagentSpec(
    key=AgentKey.AD_CAMPAIGN.value,
    name="Ad campaign",
    kind=ArtifactKind.SHEET.value,
    version=prompt.VERSION,
    system=prompt.SYSTEM_PROMPT,
    output_contract=prompt.OUTPUT_CONTRACT,
    max_tokens=lambda conf: conf.sheet_max_tokens,
    streams="snapshot",
    output_schema=RESPONSE_SCHEMA,
    validate=validate,
)
