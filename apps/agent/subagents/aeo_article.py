"""The answer-engine article agent: one checked Markdown article."""
from __future__ import annotations

from apps.agent.constants import AgentKey, ArtifactKind
from apps.agent.prompts import aeo_article as prompt
from apps.agent.subagents.base import SubagentSpec, Validated
from apps.agent.subagents.markdown_checks import check_markdown, summarize_markdown


def validate(raw: str, stop_reason: str | None) -> Validated:
    result = check_markdown(raw)
    if not result.markdown.strip():
        raise ValueError("The article came back empty.")
    warnings = list(result.warnings)
    if stop_reason == "max_tokens":
        warnings.insert(0, {"path": "document",
                            "message": "The article was cut off before it was finished."})
    return Validated(content=result.markdown, warnings=warnings,
                     summary=summarize_markdown(result.markdown))


SPEC = SubagentSpec(
    key=AgentKey.AEO_ARTICLE.value,
    name="AEO article",
    kind=ArtifactKind.DOCUMENT.value,
    version=prompt.VERSION,
    system=prompt.SYSTEM_PROMPT,
    output_contract=prompt.OUTPUT_CONTRACT,
    max_tokens=lambda conf: conf.article_max_tokens,
    streams="delta",
    output_schema=None,
    validate=validate,
)
