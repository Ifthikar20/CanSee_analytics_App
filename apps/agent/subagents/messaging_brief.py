"""The Messaging Agent: a messaging brief from the brand's context and how
AI engines describe the brand over the last 90 days (a plain document)."""
from __future__ import annotations

import json

from apps.agent.constants import AgentKey, ArtifactKind
from apps.agent.prompts import messaging_brief as prompt
from apps.agent.subagents import sourcing
from apps.agent.subagents.base import Inputs, LinkFacts, SubagentSpec, Validated
from apps.agent.subagents.markdown_checks import check_markdown, summarize_markdown

WORDS = (300, 3000)


def inputs(ctx, args, progress) -> Inputs:
    from apps.agent.services import measured

    progress("Reading how AI engines describe you")
    perception = measured.perception(ctx.website)
    return Inputs(
        blocks=[sourcing.tag("perception", json.dumps(perception, ensure_ascii=False))],
        sources={},
        link_urls=frozenset(),
        data={},
    )


def validate(raw: str, stop_reason: str | None, links: LinkFacts | None = None,
             inputs: Inputs | None = None) -> Validated:
    result = check_markdown(raw, words=WORDS, require_faq=False,
                            known_urls=links.known if links else None)
    markdown = result.markdown.strip()
    if not markdown:
        raise ValueError("The brief came back empty.")
    warnings = list(result.warnings)
    if stop_reason == "max_tokens":
        warnings.insert(0, {"path": "document",
                            "message": "The brief was cut off before it was finished."})
    return Validated(content=markdown, warnings=warnings, summary=summarize_markdown(markdown))


SPEC = SubagentSpec(
    key=AgentKey.MESSAGING_BRIEF.value,
    name="Messaging brief",
    kind=ArtifactKind.DOCUMENT.value,
    version=prompt.VERSION,
    system=prompt.SYSTEM_PROMPT,
    output_contract=prompt.OUTPUT_CONTRACT,
    max_tokens=lambda conf: conf.messaging_max_tokens,
    streams="delta",
    output_schema=None,
    validate=validate,
    checks_links=True,
    inputs=inputs,
)
