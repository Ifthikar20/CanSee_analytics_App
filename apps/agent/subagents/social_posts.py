"""The Social Posts Agent: LinkedIn posts, X posts and X threads for a
draft or a topic (data type ``social_posts``).

Lengths are counted on the server the way each network counts them
(post_length, contract 12f); text over a limit is kept, flagged and
warned, never cut. The Markdown content is built by the server from the
validated posts.
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from apps.agent.constants import AgentKey, ArtifactKind
from apps.agent.prompts import social_posts as prompt
from apps.agent.subagents import sourcing
from apps.agent.subagents.base import Inputs, LinkFacts, SubagentSpec, Validated
from apps.agent.subagents.post_length import (
    LINKEDIN_LIMIT,
    X_LIMIT,
    linkedin_length,
    x_length,
)
from apps.agent.subagents.post_length import (
    links as text_links,
)
from apps.agent.tools.context import ToolError

SOURCE_CHARS = 12_000
MAX_POSTS = 9
THREAD_PARTS = (2, 8)
DEFAULT_CHANNELS = ("linkedin", "x")
SOURCE_KINDS = ("document", "page", "report")
LABELS = {"linkedin": "LinkedIn", "x": "X", "x_thread": "X thread"}
_HREF = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


class _Output(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Post(_Output):
    channel: Literal[prompt.CHANNELS]
    parts: list[str] = []
    link_url: str = ""


class SocialOutput(_Output):
    summary: str = ""
    posts: list[Post] = []


def _html_text(html: str) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    text = "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())
    soup.decompose()
    return text


def inputs(ctx, args, progress) -> Inputs:
    from apps.agent.models import AgentArtifact

    channels = list(args.channels or DEFAULT_CHANNELS)
    blocks = [sourcing.tag("channels", ", ".join(channels))]
    source, urls = None, set()
    if args.source_artifact_id:
        artifact = (AgentArtifact.objects
                    .filter(id=args.source_artifact_id, website=ctx.website,
                            deleted_at__isnull=True, kind__in=SOURCE_KINDS, latest_version__gt=0)
                    .first())
        if artifact is None:
            raise ToolError(f"No document, page or report {args.source_artifact_id} in this "
                            "project.")
        progress(f"Reading {artifact.title}")
        version = artifact.versions.filter(version=artifact.latest_version).first()
        content = version.content if version else ""
        if artifact.kind == ArtifactKind.PAGE:
            urls.update(u for u in _HREF.findall(content) if sourcing.is_http(u))
            text = _html_text(content)
        else:
            text = content
        urls.update(u for u in text_links(text) if sourcing.is_http(u))
        source = {"artifact_id": str(artifact.id), "title": artifact.title,
                  "version": artifact.latest_version}
        blocks.append(sourcing.tag("source_artifact", text[:SOURCE_CHARS], id=artifact.id,
                                   title=artifact.title, kind=artifact.kind,
                                   version=artifact.latest_version))
    return Inputs(
        blocks=blocks,
        sources={},
        link_urls=frozenset(urls),
        data={"source": source},
        checks={"channels": channels, "title": args.title},
    )


def _length(channel: str, text: str) -> tuple[int, int]:
    if channel == "linkedin":
        return linkedin_length(text), LINKEDIN_LIMIT
    return x_length(text), X_LIMIT


def _warn(warnings: list[dict], message: str) -> None:
    if message not in [w["message"] for w in warnings]:
        warnings.append({"path": "posts", "message": message})


def validate(raw: str, stop_reason: str | None, links: LinkFacts | None = None,
             inputs: Inputs | None = None) -> Validated:
    if stop_reason == "max_tokens":
        raise ValueError("The posts were cut off before they were finished.")
    try:
        out = SocialOutput.model_validate_json(raw)
    except (ValidationError, ValueError):
        raise ValueError("The posts came back unreadable.") from None
    checks = inputs.checks if inputs else {}
    channels = checks.get("channels") or list(DEFAULT_CHANNELS)
    known = links.known if links else frozenset()

    warnings: list[dict] = []
    posts = []
    for post in out.posts:
        if post.channel not in channels:
            _warn(warnings, "A post for a network that wasn't asked for was left out.")
            continue
        parts = [p.strip() for p in post.parts if p and p.strip()]
        if not parts:
            continue
        if post.channel == "x_thread":
            low, high = THREAD_PARTS
            if len(parts) > high:
                parts = parts[:high]
                _warn(warnings, f"An X thread has at most {high} parts; the first {high} are kept.")
            elif len(parts) < low:
                _warn(warnings, f"An X thread needs {low} to {high} parts; one has {len(parts)}.")
        elif len(parts) > 1:
            parts = ["\n\n".join(parts)]
        link_url = post.link_url.strip()
        if link_url and link_url not in known and link_url.rstrip("/") not in {
                k.rstrip("/") for k in known}:
            link_url = ""
            _warn(warnings, "A post linked to an address that isn't in the sources, so the link "
                            "was left out.")
        n = len(posts) + 1
        counted = []
        for m, text in enumerate(parts, start=1):
            length, limit = _length(post.channel, text)
            counted.append({"text": text, "length": length, "limit": limit,
                            "over": length > limit})
            if length > limit:
                where = (f"X thread {n}, part {m}" if post.channel == "x_thread"
                         else f"{LABELS[post.channel]} post {n}")
                warnings.append({"path": f"posts[{n - 1}]",
                                 "message": f"{where} has {length} characters; the limit is "
                                            f"{limit}."})
        posts.append({"id": f"p{n}", "channel": post.channel, "link_url": link_url,
                      "parts": counted})
        if len(posts) >= MAX_POSTS:
            if len(out.posts) > MAX_POSTS:
                _warn(warnings, f"At most {MAX_POSTS} posts are kept.")
            break
    if not posts:
        raise ValueError("The posts came back empty.")

    title = (checks.get("title") or "").strip() or "Posts"
    content = render(title, posts)
    over = sum(1 for p in posts for part in p["parts"] if part["over"])
    counts = [f"{sum(1 for p in posts if p['channel'] == c)} {LABELS[c]}"
              for c in prompt.CHANNELS if any(p["channel"] == c for p in posts)]
    summary = f"{len(posts)} posts: {', '.join(counts)}"
    if over:
        summary += f"; {over} over the limit"
    return Validated(
        content=content,
        warnings=warnings,
        summary=summary,
        data={"type": "social_posts",
              "source": (inputs.data.get("source") if inputs else None),
              "posts": posts},
    )


def render(title: str, posts: list[dict]) -> str:
    """The posts as one Markdown document, by network."""
    lines = [f"# Social posts: {title}", ""]
    for channel in prompt.CHANNELS:
        group = [p for p in posts if p["channel"] == channel]
        if not group:
            continue
        lines += [f"## {LABELS[channel]}", ""]
        for n, post in enumerate(group, start=1):
            if len(group) > 1:
                lines += [f"### Post {n}", ""]
            if channel == "x_thread":
                lines += [f"{m}. {part['text']}" for m, part in enumerate(post["parts"], start=1)]
                lines.append("")
            else:
                lines += [post["parts"][0]["text"], ""]
            if post["link_url"]:
                lines += [f"Link: {post['link_url']}", ""]
    return "\n".join(lines).rstrip() + "\n"


SPEC = SubagentSpec(
    key=AgentKey.SOCIAL_POSTS.value,
    name="Social posts",
    kind=ArtifactKind.DOCUMENT.value,
    version=prompt.VERSION,
    system=prompt.SYSTEM_PROMPT,
    output_contract=prompt.OUTPUT_CONTRACT,
    max_tokens=lambda conf: conf.social_max_tokens,
    streams="snapshot",
    output_schema=prompt.RESPONSE_SCHEMA,
    validate=validate,
    checks_links=True,
    inputs=inputs,
    data_type="social_posts",
)
