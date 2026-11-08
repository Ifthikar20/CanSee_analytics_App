"""The Page Optimizer: one page of the site rewritten against what Cansee
measured about it, with every change listed (data type ``page_revision``).

The server reads the page and its signals before writing; a change that
quotes text the page does not have, or a metadata change whose before is
not the current value, is left out (contract 12e.2).
"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from apps.agent.constants import AgentKey, ArtifactKind
from apps.agent.prompts import page_optimizer as prompt
from apps.agent.subagents import sourcing
from apps.agent.subagents.base import (
    WRITER_BLOCK_CHARS,
    Inputs,
    LinkFacts,
    SubagentSpec,
    Validated,
)
from apps.agent.subagents.markdown_checks import check_markdown
from apps.agent.tools.context import ToolError
from apps.agent.tools.shaping import clip

PAGE_CHARS = 30_000
MAX_CHANGES = 20
WORDS = (150, 6000)
DROPPED = "A change quoted text that isn't on the page, so it was left out."


class _Output(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Change(_Output):
    kind: Literal[prompt.CHANGE_KINDS]
    section: str = ""
    before: str = ""
    after: str = ""
    reason: str = ""
    signal_ids: list[str] = []


class PageRevisionOutput(_Output):
    summary: str = ""
    title: str = ""
    meta_description: str = ""
    h1: str = ""
    changes: list[Change] = []
    page_markdown: str = ""


def inputs(ctx, args, progress) -> Inputs:
    from apps.agent.services import measured

    website = ctx.website
    domain = sourcing.apex_of(website.url or "")
    url = (args.url or "").strip()
    if not sourcing.same_site(url, website):
        raise ToolError(f"page_optimizer needs a page of {domain}.")
    progress(f"Reading {sourcing.path_label(url)} of {domain}")
    page = sourcing.read_pages([url], prefix="p", per_page_chars=PAGE_CHARS,
                               quota_user=ctx.user)[0]
    if page.error:
        raise ToolError(f"The page could not be loaded: {page.reason}")
    if page.status_code is None or not 200 <= int(page.status_code) < 300:
        raise ToolError(f"The page answered with HTTP {page.status_code}.")
    progress("Reading what Cansee measured about this page")
    measured_url = page.final_url if sourcing.same_site(page.final_url, website) else url
    signals = measured.page_signals(website, measured_url)

    seo = page.seo or {}
    current = {
        "title": seo.get("title") or "",
        "meta_description": seo.get("meta_description") or "",
        "h1": (seo.get("h1") or [""])[0],
        "structured_data": list(seo.get("structured_data") or []),
    }
    source = {"url": url, "final_url": page.final_url, "title": page.title,
              "status_code": page.status_code, "read_at": page.read_at, "words": page.words}
    signals_json = json.dumps(signals, ensure_ascii=False)
    room = max(0, WRITER_BLOCK_CHARS - len(signals_json) - 2_000)
    text = page.text[:room]
    blocks = [
        sourcing.tag("current_page", text, url=url, final_url=page.final_url,
                     title=current["title"], meta_description=current["meta_description"],
                     h1=current["h1"], words=page.words,
                     structured_data=", ".join(current["structured_data"])),
        sourcing.tag("signals", signals_json),
    ]
    links = {url, page.final_url}
    links.update(link.get("url") for link in page.links
                 if isinstance(link, dict) and link.get("url"))
    return Inputs(
        blocks=blocks,
        sources={page.id: text},
        link_urls=frozenset(u for u in links if u),
        data={"source": source, "current": current, "signals": signals},
        checks={"path": sourcing.path_label(page.final_url or url)},
    )


def validate(raw: str, stop_reason: str | None, links: LinkFacts | None = None,
             inputs: Inputs | None = None) -> Validated:
    if stop_reason == "max_tokens":
        raise ValueError("The page revision was cut off before it was finished.")
    try:
        out = PageRevisionOutput.model_validate_json(raw)
    except (ValidationError, ValueError):
        raise ValueError("The page revision came back unreadable.") from None
    data = inputs.data if inputs is not None else {}
    current = data.get("current") or {"title": "", "meta_description": "", "h1": ""}
    page_text = next(iter((inputs.sources if inputs else {}).values()), "")
    known_signals = {s["id"] for s in data.get("signals") or []}
    metadata = {sourcing.fold(current.get(k) or "") for k in ("title", "meta_description", "h1")}

    warnings: list[dict] = []
    changes, dropped = [], 0
    for change in out.changes:
        before = change.before.strip()
        if change.kind in ("rewrite", "remove"):
            before = sourcing.verbatim(page_text, before)
            if before is None:
                dropped += 1
                continue
        elif change.kind == "metadata" and sourcing.fold(before) not in metadata:
            dropped += 1
            continue
        changes.append({
            "id": f"c{len(changes) + 1}",
            "kind": change.kind,
            "section": change.section.strip(),
            "before": before,
            "after": change.after.strip(),
            "reason": change.reason.strip(),
            "signal_ids": [s for s in dict.fromkeys(change.signal_ids) if s in known_signals],
        })
    if dropped:
        warnings.append({"path": "changes", "message": DROPPED})
    if len(changes) > MAX_CHANGES:
        changes = changes[:MAX_CHANGES]
        warnings.append({"path": "changes",
                         "message": f"The revision listed more than {MAX_CHANGES} changes; the "
                                    f"first {MAX_CHANGES} are kept."})

    result = check_markdown(out.page_markdown, words=WORDS, require_faq=False,
                            known_urls=links.known if links else None)
    markdown = result.markdown.strip()
    if not markdown:
        raise ValueError("The revised page came back empty.")
    if not markdown.startswith("# "):
        raise ValueError("The revised page has to start with its H1 (a line that starts "
                         "with \"# \").")
    warnings += [w for w in result.warnings if w["message"] != "The article has no H1."]
    path = (inputs.checks.get("path") if inputs else "") or "the page"
    summary = clip(f"{len(changes)} changes to {path}. {out.summary.strip()}".strip(), 500)
    return Validated(
        content=markdown,
        warnings=warnings,
        summary=summary,
        data={
            "type": "page_revision",
            "source": data.get("source"),
            "current": current,
            "proposed": {"title": out.title.strip(),
                         "meta_description": out.meta_description.strip(),
                         "h1": out.h1.strip()},
            "changes": changes,
            "signals": data.get("signals") or [],
        },
    )


SPEC = SubagentSpec(
    key=AgentKey.PAGE_OPTIMIZER.value,
    name="Page optimizer",
    kind=ArtifactKind.DOCUMENT.value,
    version=prompt.VERSION,
    system=prompt.SYSTEM_PROMPT,
    output_contract=prompt.OUTPUT_CONTRACT,
    max_tokens=lambda conf: conf.optimizer_max_tokens,
    streams="snapshot",
    output_schema=prompt.RESPONSE_SCHEMA,
    validate=validate,
    checks_links=True,
    inputs=inputs,
    data_type="page_revision",
)
