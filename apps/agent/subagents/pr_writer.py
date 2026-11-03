"""The PR Agent: a press release, a media pitch, or correction requests to
the publishers whose pages repeat a claim the brand's evidence
contradicts (data type ``press`` or ``corrections``).

For corrections the server reads the Fact check claim and the
third-party pages its answers cite (taken from stored citations, never
from model output); a letter whose quote is not found on its page is
dropped and the page listed as not repeating the claim (contract 12e.4).
"""
from __future__ import annotations

import json
import re

from pydantic import BaseModel, ConfigDict, ValidationError

from apps.agent.conf import agent_conf
from apps.agent.constants import AgentKey, ArtifactKind
from apps.agent.prompts import pr_writer as prompt
from apps.agent.subagents import sourcing
from apps.agent.subagents.base import (
    WRITER_BLOCK_CHARS,
    Inputs,
    LinkFacts,
    SubagentSpec,
    Validated,
)
from apps.agent.subagents.markdown_checks import check_markdown, summarize_markdown
from apps.agent.tools.context import ToolError

PAGE_CHARS = 30_000
WORDS = {"press_release": (200, 1200), "media_pitch": (60, 350)}
CORRECTION = "correction_request"
QUOTE_WARNING = "This quote isn't in your brief or context; check that it was said."
NO_EVIDENCE_LINK = "This letter doesn't link your evidence."
# A quoted sentence: at least a few words between straight or curly quotes.
_QUOTED = re.compile(r"[\"“]([^\"“”\n]{12,})[\"”]")


class _Output(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Target(_Output):
    source_id: str = ""
    quote: str = ""
    letter_markdown: str = ""


class PressOutput(_Output):
    summary: str = ""
    markdown: str = ""
    targets: list[Target] = []


def data_type(args) -> str:
    return "corrections" if args.format == CORRECTION else "press"


def inputs(ctx, args, progress) -> Inputs:
    from apps.agent.services.context import agent_context

    website = ctx.website
    fmt = args.format
    known_text = "\n".join([args.title, args.brief, *args.facts,
                            json.dumps(agent_context(website), ensure_ascii=False)])
    blocks = [sourcing.tag("format", fmt)]
    if fmt != CORRECTION:
        return Inputs(blocks=blocks, sources={}, link_urls=frozenset(),
                      data={"format": fmt}, checks={"format": fmt, "known_text": known_text})
    return _correction_inputs(ctx, args, progress, blocks, known_text)


def _correction_inputs(ctx, args, progress, blocks, known_text) -> Inputs:
    from apps.agent.services import measured

    website = ctx.website
    progress("Reading the Fact check claim")
    claim = measured.correction_claim(website, args.claim_key)
    if claim is None:
        raise ToolError(f"No Fact check claim {args.claim_key} in this project.")
    if claim.get("verdict") != "contradicted":
        raise ToolError("Correction requests are for claims your evidence contradicts.")
    cited = [u for u in claim.get("cited_urls") or []
             if isinstance(u, dict) and sourcing.is_http(u.get("url") or "")
             and not sourcing.same_site(u["url"], website)]
    cited = cited[:agent_conf().correction_max_pages]
    if not cited:
        raise ToolError("The answers that state this claim cite no pages outside your site, so "
                        "there is no publisher to write to.")
    progress(f"Reading {len(cited)} pages the engines cited")
    pages = sourcing.read_pages([u["url"] for u in cited], prefix="t",
                                per_page_chars=PAGE_CHARS, quota_user=ctx.user)
    evidence = claim.get("evidence") or {}
    evidence = {"kind": evidence.get("kind"), "url": evidence.get("url") or "",
                "quote": evidence.get("quote") or ""}
    summary = {
        "key": claim.get("key") or args.claim_key,
        "text": claim.get("text") or "",
        "engines": claim.get("engines") or [],
        "answers": claim.get("answers"),
        "evidence": evidence,
    }
    said = [o.get("quote") for o in claim.get("occurrences") or [] if o.get("quote")]
    claim_json = json.dumps({**{k: summary[k] for k in ("key", "text", "engines", "answers")},
                             "answer_quotes": said}, ensure_ascii=False)
    evidence_json = json.dumps(evidence, ensure_ascii=False)
    read = [p for p in pages if p.ok]
    texts = sourcing.water_fill(
        [p.text for p in read],
        max(0, WRITER_BLOCK_CHARS - len(claim_json) - len(evidence_json) - 2_000))
    blocks += [sourcing.tag("claim", claim_json), sourcing.tag("evidence", evidence_json)]
    sources = {}
    for page, text in zip(read, texts, strict=True):
        blocks.append(sourcing.tag("third_party_page", text, id=page.id, url=page.final_url,
                                   publisher=sourcing.apex_of(page.final_url or page.url)))
        sources[page.id] = text
    targets = [{"id": p.id, "url": p.final_url or p.url,
                "publisher": sourcing.apex_of(p.final_url or p.url), "read_at": p.read_at,
                "ok": p.ok, "reason": p.reason} for p in pages]
    links = {u for p in read for u in (p.url, p.final_url) if u}
    if evidence["url"]:
        links.add(evidence["url"])
    return Inputs(
        blocks=blocks,
        sources=sources,
        link_urls=frozenset(links),
        data={"claim": summary},
        checks={"format": CORRECTION, "targets": targets, "known_text": known_text},
    )


def validate(raw: str, stop_reason: str | None, links: LinkFacts | None = None,
             inputs: Inputs | None = None) -> Validated:
    if stop_reason == "max_tokens":
        raise ValueError("The draft was cut off before it was finished.")
    try:
        out = PressOutput.model_validate_json(raw)
    except (ValidationError, ValueError):
        raise ValueError("The draft came back unreadable.") from None
    checks = inputs.checks if inputs else {}
    known = links.known if links else None
    if checks.get("format") == CORRECTION:
        return _corrections(out, inputs, known)

    fmt = checks.get("format") or "press_release"
    result = check_markdown(out.markdown, words=WORDS.get(fmt, WORDS["press_release"]),
                            require_faq=False, known_urls=known)
    markdown = result.markdown.strip()
    if not markdown:
        raise ValueError("The draft came back empty.")
    warnings = list(result.warnings)
    known_text = checks.get("known_text") or ""
    quotes = [q.strip().rstrip(".,;:!?").strip() for q in _QUOTED.findall(markdown)]
    if any(q and not sourcing.found(known_text, q) for q in quotes):
        warnings.append({"path": "quotes", "message": QUOTE_WARNING})
    return Validated(content=markdown, warnings=warnings, summary=summarize_markdown(markdown),
                     data={"type": "press", "format": fmt})


def _corrections(out: PressOutput, inputs: Inputs, known) -> Validated:
    claim = inputs.data.get("claim") or {}
    evidence = claim.get("evidence") or {}
    pages = inputs.checks.get("targets") or []
    readable = {p["id"]: p for p in pages if p["ok"]}
    warnings: list[dict] = []
    kept = []
    for target in out.targets:
        source_id = target.source_id.strip()
        page = readable.get(source_id)
        if page is None or any(k["id"] == source_id for k in kept):
            continue
        quote = sourcing.verbatim(inputs.sources.get(source_id, ""), target.quote)
        if quote is None:
            continue
        letter = check_markdown(target.letter_markdown, words=None, require_faq=False,
                                require_h1=False, known_urls=known)
        text = letter.markdown.strip()
        if not text:
            continue
        for warning in letter.warnings:
            if warning["message"] not in [w["message"] for w in warnings]:
                warnings.append(warning)
        if (evidence.get("kind") == "page" and evidence.get("url")
                and evidence["url"].rstrip("/") not in text):
            if NO_EVIDENCE_LINK not in [w["message"] for w in warnings]:
                warnings.append({"path": "targets", "message": NO_EVIDENCE_LINK})
        kept.append({"id": source_id, "url": page["url"], "publisher": page["publisher"],
                     "quote": quote, "read_at": page["read_at"], "letter": text})
    kept_ids = {k["id"] for k in kept}
    not_repeating = [{"url": p["url"], "publisher": p["publisher"]}
                     for p in pages if p["ok"] and p["id"] not in kept_ids]
    unreadable = [{"url": p["url"], "publisher": p["publisher"], "reason": p["reason"]}
                  for p in pages if not p["ok"]]
    content = render(claim, kept, not_repeating, unreadable)
    summary = (f"{len(kept)} correction letters; {len(not_repeating)} pages don't repeat the "
               f"claim; {len(unreadable)} couldn't be read")
    return Validated(
        content=content,
        warnings=warnings,
        summary=summary,
        data={"type": "corrections", "claim": claim, "targets": kept,
              "not_repeating": not_repeating, "unreadable": unreadable},
    )


def render(claim: dict, kept: list[dict], not_repeating: list[dict],
           unreadable: list[dict]) -> str:
    lines = [f'# Correction requests: "{claim.get("text") or ""}"', ""]
    for target in kept:
        lines += [f"## {target['publisher']}", "", f"Page: {target['url']}", "",
                  f'What it says: "{target["quote"]}"', "", target["letter"], ""]
    if not_repeating:
        lines += ["## Pages that don't repeat the claim", ""]
        lines += [f"- {p['url']} ({p['publisher']})" for p in not_repeating]
        lines.append("")
    if unreadable:
        lines += ["## Pages that couldn't be read", ""]
        lines += [f"- {p['url']} ({p['publisher']}): {p['reason']}" for p in unreadable]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


SPEC = SubagentSpec(
    key=AgentKey.PR_WRITER.value,
    name="Press",
    kind=ArtifactKind.DOCUMENT.value,
    version=prompt.VERSION,
    system=prompt.SYSTEM_PROMPT,
    output_contract=prompt.OUTPUT_CONTRACT,
    max_tokens=lambda conf: conf.pr_max_tokens,
    streams="snapshot",
    output_schema=prompt.RESPONSE_SCHEMA,
    validate=validate,
    checks_links=True,
    inputs=inputs,
    data_type=data_type,
)
