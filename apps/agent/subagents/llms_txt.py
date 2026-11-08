"""The llms.txt Agent: an llms.txt file for the project's site, linking only
to pages Cansee knows on it (data type ``llms_txt``). Cansee never
publishes the file; the user does."""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from apps.agent.constants import AgentKey, ArtifactKind
from apps.agent.prompts import llms_txt as prompt
from apps.agent.subagents import sourcing
from apps.agent.subagents.base import Inputs, LinkFacts, SubagentSpec, Validated
from apps.agent.subagents.markdown_checks import check_markdown

HOME_CHARS = 2_000
NO_TITLE = "The file needs a title line (# Name)."
_LINKS = re.compile(r"\[[^\]]*\]\(\s*https?://[^)\s]+")
_SECTION = re.compile(r"^##\s+\S", re.MULTILINE)


def _origin(url: str) -> str:
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return ""
    return f"{parts.scheme.lower()}://{(parts.hostname or '').lower()}" if parts.hostname else ""


def inputs(ctx, args, progress) -> Inputs:
    from apps.agent.services import measured

    website = ctx.website
    domain = sourcing.apex_of(website.url or "")
    progress(f"Reading the pages Cansee knows on {domain}")
    home = sourcing.read_pages([website.url], prefix="p", per_page_chars=HOME_CHARS,
                               quota_user=ctx.user, with_links=True)[0]
    pages = measured.site_pages(website, limit=60, home=home)
    rows = [{"id": f"p{n}", "url": p["url"], "title": p["title"]}
            for n, p in enumerate(pages, start=1)]
    return Inputs(
        blocks=[sourcing.tag("site_pages", json.dumps(rows, ensure_ascii=False))],
        sources={},
        link_urls=frozenset(p["url"] for p in pages),
        data={"origin": _origin(website.url)},
        checks={"site_urls": frozenset(p["url"] for p in pages)},
    )


def validate(raw: str, stop_reason: str | None, links: LinkFacts | None = None,
             inputs: Inputs | None = None) -> Validated:
    site_urls = (inputs.checks.get("site_urls") if inputs else None) or frozenset()
    result = check_markdown(raw, words=None, require_faq=False, require_h1=False,
                            known_urls=site_urls)
    text = result.markdown.strip()
    if not text:
        raise ValueError("The file came back empty.")
    if not text.splitlines()[0].startswith("# "):
        raise ValueError(NO_TITLE)
    warnings = [{**w, "message": w["message"].replace("around the article", "around the file")}
                for w in result.warnings]
    if stop_reason == "max_tokens":
        warnings.insert(0, {"path": "document",
                            "message": "The file was cut off before it was finished."})
    links_count = len(_LINKS.findall(text))
    sections = len(_SECTION.findall(text))
    origin = (inputs.data.get("origin") if inputs else "") or ""
    return Validated(
        content=text + "\n",
        warnings=warnings,
        summary=f"llms.txt with {sections} sections and {links_count} links",
        data={"type": "llms_txt", "origin": origin, "links": links_count, "sections": sections},
    )


SPEC = SubagentSpec(
    key=AgentKey.LLMS_TXT.value,
    name="llms.txt",
    kind=ArtifactKind.DOCUMENT.value,
    version=prompt.VERSION,
    system=prompt.SYSTEM_PROMPT,
    output_contract=prompt.OUTPUT_CONTRACT,
    max_tokens=lambda conf: conf.llms_txt_max_tokens,
    streams="delta",
    output_schema=None,
    validate=validate,
    checks_links=True,
    inputs=inputs,
    data_type="llms_txt",
)
