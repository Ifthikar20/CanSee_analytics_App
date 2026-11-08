"""The Comparison Page Agent: one page comparing the brand with a tracked
competitor, from what each side's own pages say (data type ``comparison``).

The server reads the competitor's home page (and one page the call
names), the brand's home page and up to two pages its Offerings entries
cite. A statement about either side needs a quote found word for word in
a page of that side; one without stays in the text, marked not verified
and warned (contract 12e.3).
"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from apps.agent.conf import agent_conf
from apps.agent.constants import AgentKey, ArtifactKind
from apps.agent.prompts import comparison_page as prompt
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
WORDS = (400, 4000)
BENCHMARK_RULE = ("Internal measurements: use them to choose which questions the page answers; "
                  "never put them on the page.")


class _Output(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Claim(_Output):
    about: Literal["you", "competitor"]
    text: str = ""
    source_id: str = ""
    quote: str = ""


class ComparisonOutput(_Output):
    summary: str = ""
    markdown: str = ""
    claims: list[Claim] = []


def _domain_of(value: str) -> str:
    value = (value or "").strip().lower()
    if "://" in value:
        value = sourcing.host(value)
    value = value.split("/", 1)[0].strip(".")
    return value[4:] if value.startswith("www.") else value


def find_competitor(website, wanted: str) -> dict | None:
    """The tracked competitor ``wanted`` names, by its domain or its name."""
    wanted = (wanted or "").strip()
    apex = sourcing.apex_of(_domain_of(wanted)) if "." in wanted else ""
    for c in website.competitors or []:
        if not isinstance(c, dict) or not (c.get("name") or "").strip():
            continue
        domain = _domain_of(c.get("domain") or "")
        if (apex and domain and sourcing.apex_of(domain) == apex) or (
                sourcing.fold(c["name"]) == sourcing.fold(wanted)):
            return {"name": c["name"].strip(), "domain": domain}
    return None


def inputs(ctx, args, progress) -> Inputs:
    from apps.agent.services import measured
    from apps.agent.services.context import offering_urls

    website = ctx.website
    competitor = find_competitor(website, args.competitor or "")
    if competitor is None:
        raise ToolError(f"{args.competitor} isn't one of the competitors this project tracks.")
    name, domain = competitor["name"], competitor["domain"]
    theirs = [f"https://{domain}/"] if domain else []
    if args.url:
        url = args.url.strip()
        if not domain or sourcing.apex_of(url) != sourcing.apex_of(domain):
            raise ToolError(f"{url} isn't a page of {domain or name}.")
        if url.rstrip("/") != theirs[0].rstrip("/"):
            theirs.append(url)
    room = max(1, agent_conf().writer_max_pages)
    theirs = theirs[:room]
    ours = [website.url, *offering_urls(website)[:2]][:max(0, room - len(theirs))]
    ours = list(dict.fromkeys(u for u in ours if u))
    progress(f"Reading {len(theirs) + len(ours)} pages of {domain or name} and "
             f"{sourcing.apex_of(website.url or '')}")
    their_pages = sourcing.read_pages(theirs, prefix="c", per_page_chars=PAGE_CHARS,
                                      quota_user=ctx.user)
    if not any(p.ok for p in their_pages):
        raise ToolError(f"None of {name}'s pages could be read.")
    our_pages = sourcing.read_pages(ours, prefix="p", per_page_chars=PAGE_CHARS,
                                    quota_user=ctx.user)
    progress(f"Reading the benchmark with {name}")
    benchmark = measured.competitor_benchmark(website, competitor)
    bench_text = f"{BENCHMARK_RULE}\n{json.dumps(benchmark, ensure_ascii=False)}"

    read = [p for p in [*their_pages, *our_pages] if p.ok]
    texts = sourcing.water_fill([p.text for p in read],
                                max(0, WRITER_BLOCK_CHARS - len(bench_text) - 2_000))
    blocks, sources, owners = [], {}, {}
    for page, text in zip(read, texts, strict=True):
        owner = "competitor" if page.id.startswith("c") else "you"
        tag = "competitor_page" if owner == "competitor" else "your_page"
        blocks.append(sourcing.tag(tag, text, id=page.id, url=page.final_url, title=page.title))
        sources[page.id] = text
        owners[page.id] = owner
    blocks.append(sourcing.tag("benchmark", bench_text))
    listed = [
        {"id": p.id, "owner": "competitor" if p.id.startswith("c") else "you", "url": p.final_url,
         "title": p.title, "read_at": p.read_at, "state": "ok" if p.ok else "failed"}
        for p in [*their_pages, *our_pages]
    ]
    return Inputs(
        blocks=blocks,
        sources=sources,
        link_urls=frozenset(u for p in read for u in (p.url, p.final_url) if u),
        data={"competitor": competitor, "sources": listed},
        checks={"owners": owners, "names": {"you": website.name, "competitor": name}},
    )


def validate(raw: str, stop_reason: str | None, links: LinkFacts | None = None,
             inputs: Inputs | None = None) -> Validated:
    if stop_reason == "max_tokens":
        raise ValueError("The comparison page was cut off before it was finished.")
    try:
        out = ComparisonOutput.model_validate_json(raw)
    except (ValidationError, ValueError):
        raise ValueError("The comparison page came back unreadable.") from None
    sources = inputs.sources if inputs else {}
    checks = inputs.checks if inputs else {}
    owners, names = checks.get("owners") or {}, checks.get("names") or {}

    warnings: list[dict] = []
    claims = []
    for claim in out.claims:
        source_id = claim.source_id.strip()
        quote = sourcing.verbatim(sources.get(source_id, ""), claim.quote)
        verified = quote is not None and owners.get(source_id) == claim.about
        claims.append({"about": claim.about, "text": claim.text.strip(), "source_id": source_id,
                       "quote": quote if quote is not None else claim.quote.strip(),
                       "verified": verified})
        if not verified:
            message = (f"This statement about {names.get(claim.about) or claim.about} isn't "
                       "backed by a quote from its source.")
            if message not in [w["message"] for w in warnings]:
                warnings.append({"path": "claims", "message": message})

    result = check_markdown(out.markdown, words=WORDS, require_faq=False,
                            known_urls=links.known if links else None)
    markdown = result.markdown.strip()
    if not markdown:
        raise ValueError("The comparison page came back empty.")
    data = inputs.data if inputs else {}
    verified = sum(1 for c in claims if c["verified"])
    return Validated(
        content=markdown,
        warnings=result.warnings + warnings,
        summary=f"{summarize_markdown(markdown)}; {verified} of {len(claims)} claims verified",
        data={"type": "comparison", "competitor": data.get("competitor"),
              "sources": data.get("sources") or [], "claims": claims},
    )


SPEC = SubagentSpec(
    key=AgentKey.COMPARISON_PAGE.value,
    name="Comparison page",
    kind=ArtifactKind.DOCUMENT.value,
    version=prompt.VERSION,
    system=prompt.SYSTEM_PROMPT,
    output_contract=prompt.OUTPUT_CONTRACT,
    max_tokens=lambda conf: conf.comparison_max_tokens,
    streams="snapshot",
    output_schema=prompt.RESPONSE_SCHEMA,
    validate=validate,
    checks_links=True,
    inputs=inputs,
    data_type="comparison",
)
