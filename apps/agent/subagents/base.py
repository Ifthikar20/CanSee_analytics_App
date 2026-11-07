"""Running one writing agent for ``run_agent``.

One streamed request with no tools. The user content is a sequence of
XML-tagged data blocks (<brand>, <target_prompt>, the blocks a writer
reads before it writes, <current_version>, <brief>, <output_contract>).
Text streams to the pane as ``artifact.delta`` (pages, articles) or
throttled ``artifact.snapshot`` partial JSON (sheets, structured drafts);
the result is validated, saved as the artifact's next version, and
summarized for the orchestrator. Spend is checked before the request: the
writer's ``max_tokens`` is what the user's remaining allowance pays for at
$20 per MTok, at most the agent's own limit, and a writer that would get
less than MIN_WRITER_TOKENS does not start.

The writers of design/p2_backend.md 12 read their sources first
(``SubagentSpec.inputs``: pages of the site, measurements, a claim), with
each source tagged by an id the validator checks quotes against; a
structured draft keeps its parts in the version's ``data``.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

from django.db import transaction

from apps.agent.constants import EventType, VersionSource
from apps.agent.llm.client import get_client
from apps.agent.llm.errors import classify
from apps.agent.llm.gate import CancelRequested, GateStop
from apps.agent.llm.request import build_subagent_request
from apps.agent.llm.usage import estimate_output_tokens, record_request_usage
from apps.agent.runtime.translate import DeltaBuffer
from apps.agent.services.catalog import agent_name
from apps.agent.tools.context import ToolContext, ToolError, ToolResult
from apps.agent.tools.shaping import clip

logger = logging.getLogger("apps")

# Worst case output price used to size a writer's request ($ per token).
OUTPUT_PRICE_PER_TOKEN = Decimal("20") / Decimal("1000000")
# The smallest output budget a writer is started with ($0.32).
MIN_WRITER_TOKENS = 16_000
SNAPSHOT_SECONDS = 0.5
PROGRESS_SECONDS = 1.0
CURRENT_VERSION_CHARS = 120_000
# Characters the blocks a writer reads before it writes hold together
# (pages, signals, the source artifact, a claim and its pages), so a new
# draft's user turn stays under 80,000 characters (contract 12c).
WRITER_BLOCK_CHARS = 40_000
# The most a new draft's user turn costs as input: 80,000 characters at 3
# per token and $4 per million, plus writing the system prompt to the cache.
WRITER_INPUT_USD_MAX = Decimal("0.13")
# The data blocks whose URLs a writer may link to. Never <current_version>:
# that is the writer's own earlier output, not a source.
_LINK_SOURCES = ("brand", "target_prompt", "brief")
_LINK = re.compile(r"https?://[^\s\"'<>\\]+", re.IGNORECASE)


@dataclass(frozen=True)
class Validated:
    content: str
    warnings: list[dict]
    summary: str
    # A structured draft's parts (contract 12d), saved as the version's data.
    data: dict | None = None


@dataclass(frozen=True)
class Inputs:
    """What a writer read before writing (``SubagentSpec.inputs``)."""

    # Data blocks inserted after <brand> and <target_prompt>.
    blocks: list[dict]
    # Source id -> the text the validator checks that source's quotes in.
    sources: dict[str, str]
    # URLs of those sources the draft may link to.
    link_urls: frozenset[str]
    # The parts read before writing that streamed snapshots carry (source,
    # current, signals, competitor, sources, claim).
    data: dict
    # What only the validator needs (the requested channels, the pages read).
    checks: dict = field(default_factory=dict)


@dataclass(frozen=True)
class LinkFacts:
    """What a writer may link to: the brand's website and the URLs its
    data blocks carry."""
    site_url: str
    known: frozenset[str]


@dataclass(frozen=True)
class SubagentSpec:
    key: str
    name: str
    kind: str
    version: str
    system: str
    output_contract: str
    max_tokens: Callable
    # "delta": text streams as artifact.delta; "snapshot": partial JSON.
    streams: str
    output_schema: dict | None
    # Raises ValueError with a user-readable message when the output is unusable.
    # Called as validate(raw, stop_reason), plus links=LinkFacts when
    # checks_links and inputs=Inputs when the spec has ``inputs``.
    validate: Callable[..., Validated]
    checks_links: bool = False
    # Reads the writer's sources before the artifact exists:
    # inputs(ctx, args, progress) -> Inputs, where progress(line) sets the
    # part's progress line. A ToolError there fails the call.
    inputs: Callable[[ToolContext, object, Callable[[str], None]], Inputs] | None = None
    # data.type of a structured draft (contract 12d), or a function of the
    # call's args (pr_writer: press or corrections).
    data_type: str | Callable[[object], str] | None = None


def _specs() -> dict[str, SubagentSpec]:
    from apps.agent.subagents import (
        ad_campaign,
        aeo_article,
        comparison_page,
        landing_page,
        llms_txt,
        messaging_brief,
        page_optimizer,
        pr_writer,
        social_posts,
    )

    return {s.key: s for s in (
        landing_page.SPEC, aeo_article.SPEC, ad_campaign.SPEC,
        page_optimizer.SPEC, llms_txt.SPEC, comparison_page.SPEC, social_posts.SPEC,
        pr_writer.SPEC, messaging_brief.SPEC,
    )}


def data_type_of(spec: SubagentSpec, args) -> str | None:
    if callable(spec.data_type):
        return spec.data_type(args)
    return spec.data_type


def _block(tag: str, body: str) -> dict:
    return {"type": "text", "text": f"<{tag}>\n{body}\n</{tag}>"}


def build_user_content(ctx: ToolContext, spec: SubagentSpec, args, current: str | None,
                       extra: Inputs | None = None) -> list:
    from apps.agent.tools.data import brand_facts, trim_prompt_detail

    brand = brand_facts(ctx.website)
    if args.facts:
        # Passed on by the orchestrator from this chat; nobody approved them.
        brand["facts_from_chat"] = list(args.facts)
    blocks = [_block("brand", json.dumps(brand, ensure_ascii=False, indent=1, sort_keys=True))]
    if args.prompt_id:
        from apps.prompt_library.models import BrandPrompt
        from apps.search_gap.services.overview import build_prompt_detail

        bp = (BrandPrompt.objects.select_related("prompt")
              .filter(website=ctx.website, prompt_id=args.prompt_id).first())
        if bp is None:
            raise ToolError(f"No tracked prompt with prompt_id {args.prompt_id} in this project.",
                            summary="Prompt not found")
        detail = trim_prompt_detail(build_prompt_detail(ctx.website, bp))
        blocks.append(_block("target_prompt", json.dumps(detail, ensure_ascii=False, indent=1,
                                                         sort_keys=True)))
    if extra is not None:
        blocks.extend(extra.blocks)
    if current is not None:
        blocks.append(_block("current_version", current[:CURRENT_VERSION_CHARS]))
    blocks.append(_block("brief", f"Title: {args.title}\n\n{args.brief}"))
    blocks.append(_block("output_contract", spec.output_contract))
    return blocks


def link_facts(website, blocks: list[dict], extra: Inputs | None = None) -> LinkFacts:
    """The website and every URL in the <brand>, <target_prompt> and
    <brief> blocks of ``blocks`` (build_user_content's), plus the URLs of
    the sources the writer read (``extra.link_urls``)."""
    heads = tuple(f"<{tag}>\n" for tag in _LINK_SOURCES)
    known = {website.url} if website.url else set()
    for block in blocks:
        text = block.get("text") or ""
        if text.startswith(heads):
            known.update(url.rstrip(".,;:!?)]}") for url in _LINK.findall(text))
    if extra is not None:
        known.update(extra.link_urls)
    return LinkFacts(site_url=website.url or "", known=frozenset(known))


def writer_budget(user, limit: int) -> tuple[int, int, float, float]:
    """(max_tokens, minimum, cap, spent) for a writer whose own output
    limit is ``limit`` tokens.

    ``max_tokens`` is what the user's remaining allowance pays for at the
    worst case output price, at most ``limit`` (all of it when the
    allowance is unlimited). A writer whose ``max_tokens`` is below
    ``minimum`` (MIN_WRITER_TOKENS, or ``limit`` when smaller) is not
    worth starting.
    """
    from core.ai_tracking import effective_ai_cap, month_to_date_cost

    minimum = min(limit, MIN_WRITER_TOKENS)
    cap = float(effective_ai_cap(user))
    if cap <= 0:
        return limit, minimum, cap, 0.0
    spent = float(month_to_date_cost(user))
    left = Decimal(str(max(0.0, cap - spent)))
    return min(limit, int(left / OUTPUT_PRICE_PER_TOKEN)), minimum, cap, spent


class _Progress:
    """Turns streamed thinking into a short progress line on the part."""

    def __init__(self, emitter, part_id: str):
        self.em = emitter
        self.part_id = part_id
        self.text = ""
        self.last = 0.0

    def add(self, text: str) -> None:
        self.text = (self.text + text)[-2000:]
        now = time.monotonic()
        if now - self.last >= PROGRESS_SECONDS:
            self.last = now
            line = self.text.strip().splitlines()[-1] if self.text.strip() else ""
            if line:
                self.em.update_part(self.part_id, progress=clip(line, 140))


def _save_version(ctx: ToolContext, spec: SubagentSpec, *, artifact_id, existing: bool,
                  title: str, validated: Validated, cost: Decimal) -> int:
    """Create the artifact (first version) and append the next version
    through services.artifacts.add_version, the one version writer."""
    from apps.agent.models import AgentArtifact
    from apps.agent.services.artifacts import add_version

    with transaction.atomic():
        if existing:
            artifact = AgentArtifact.objects.select_for_update().get(id=artifact_id)
        else:
            artifact = AgentArtifact.objects.create(
                id=artifact_id, website=ctx.website, kind=spec.kind, agent_key=spec.key,
                title=title, created_by=ctx.user, source_chat=ctx.chat,
                source_message_id=ctx.run.assistant_message_id,
            )
        version = add_version(
            artifact,
            content=validated.content,
            data=validated.data,
            summary=validated.summary,
            warnings=validated.warnings,
            source=VersionSource.AGENT,
            created_by=ctx.user,
            run=ctx.run,
            cost_usd=cost,
        )
        artifact.title = title
        artifact.deleted_at = None
        artifact.save(update_fields=["title", "deleted_at", "updated_at"])
    return version.version


def run_subagent(ctx: ToolContext, args) -> ToolResult:
    from apps.agent.models import AgentArtifact

    spec = _specs()[args.agent]
    em = ctx.emitter
    title = clip(args.title, 200)
    display_name = agent_name(spec.key)
    part_id = ctx.part_id or em.add_part(
        "agent_call", request=ctx.request, agent=spec.key, agent_name=display_name,
        status="preparing", artifact_id=None, version=None, title=title, kind=spec.kind,
        summary="", error=None,
    )
    em.update_part(part_id, agent=spec.key, agent_name=display_name, title=title, kind=spec.kind)

    def fail(message: str, *, code: str, artifact_id=None, status: str = "error") -> ToolError:
        em.update_part(part_id, status=status, error=message)
        if artifact_id is not None:
            em.emit(EventType.ARTIFACT_FAILED, artifact_id=str(artifact_id),
                    error={"code": code, "message": message})
        return ToolError(message, summary=message)

    from apps.agent.conf import agent_conf

    limit = int(spec.max_tokens(agent_conf()))
    max_tokens, minimum, cap, spent = writer_budget(ctx.user, limit)
    if max_tokens < minimum:
        raise fail(
            f"Insufficient allowance: the {spec.name.lower()} agent needs at least "
            f"${float(OUTPUT_PRICE_PER_TOKEN * minimum):.2f} and "
            f"${max(0.0, cap - spent):.2f} of the ${cap:.2f} monthly AI allowance is left.",
            code="insufficient_allowance",
        )

    existing = None
    current = None
    if args.artifact_id:
        existing = (AgentArtifact.objects
                    .filter(id=args.artifact_id, website=ctx.website, deleted_at__isnull=True)
                    .first())
        if existing is None:
            raise fail(f"No artifact {args.artifact_id} in this project.", code="not_found")
        if existing.kind != spec.kind:
            raise fail(f"Artifact {args.artifact_id} is a {existing.kind}, not a {spec.kind}.",
                       code="wrong_kind")
        if existing.agent_key and existing.agent_key != spec.key:
            # Seven agents write documents: one never overwrites another's draft.
            raise fail(f"Artifact {args.artifact_id} was written by the "
                       f"{agent_name(existing.agent_key)}; ask that agent to revise it.",
                       code="wrong_kind")
        latest = existing.versions.order_by("-version").first()
        current = latest.content if latest else None
    artifact_id = existing.id if existing else uuid.uuid4()
    base_version = existing.latest_version if existing else 0

    extra_inputs = None
    if spec.inputs is not None:
        def set_progress(line: str) -> None:
            em.update_part(part_id, progress=clip(line, 140))

        try:
            extra_inputs = spec.inputs(ctx, args, set_progress)
        except ToolError as exc:
            raise fail(exc.message, code="invalid_input") from None
    data_type = data_type_of(spec, args)

    content_blocks = build_user_content(ctx, spec, args, current, extra_inputs)
    if ctx.check_gate is not None:
        ctx.check_gate()

    em.emit(EventType.ARTIFACT_STARTED, artifact_id=str(artifact_id), kind=spec.kind,
            title=title, agent_key=spec.key, base_version=base_version)
    # base_version > 0: the artifact was saved before this call (a revision,
    # possibly of an artifact another chat made), so a failure leaves it as it was.
    em.update_part(part_id, status="planning", artifact_id=str(artifact_id),
                   base_version=base_version)

    request = build_subagent_request(spec, content_blocks, max_tokens=max_tokens)
    progress = _Progress(em, part_id)
    text_parts: list[str] = []
    state = {"drafting": False, "last_snapshot": 0.0}
    delta = DeltaBuffer(lambda text: em.emit(EventType.ARTIFACT_DELTA,
                                             artifact_id=str(artifact_id), append=text))

    def on_text(text: str) -> None:
        if not state["drafting"]:
            state["drafting"] = True
            em.update_part(part_id, status="drafting")
        text_parts.append(text)
        if spec.streams == "delta":
            delta.add(text)
            return
        now = time.monotonic()
        if now - state["last_snapshot"] >= SNAPSHOT_SECONDS:
            state["last_snapshot"] = now
            _emit_snapshot()

    def _emit_snapshot() -> None:
        from jiter import from_json

        raw = "".join(text_parts).encode("utf-8")
        try:
            partial = from_json(raw, partial_mode="trailing-strings")
        except ValueError:
            return
        if data_type is not None:
            # The parts the server read come first, so the pane can pick the
            # structured view before the draft is saved (contract 12d).
            partial = {**(extra_inputs.data if extra_inputs is not None else {}),
                       **(partial if isinstance(partial, dict) else {}),
                       "type": data_type, "drafting": True}
        em.emit(EventType.ARTIFACT_SNAPSHOT, artifact_id=str(artifact_id), content=partial)

    final = None
    error = None
    started = time.monotonic()
    stream = None
    start_usage = None
    request_id = ""
    chars = 0
    try:
        with get_client().beta.messages.stream(**request) as stream:
            ctx.active.attach(stream)
            request_id = stream.request_id or ""
            for event in stream:
                if ctx.cancelled():
                    break
                if event.type == "message_start":
                    usage = getattr(event.message, "usage", None)
                    start_usage = usage.to_dict(mode="json") if usage is not None else None
                elif event.type == "content_block_delta":
                    kind = event.delta.type
                    if kind == "thinking_delta" and event.delta.thinking:
                        chars += len(event.delta.thinking)
                        progress.add(event.delta.thinking)
                    elif kind == "text_delta" and event.delta.text:
                        chars += len(event.delta.text)
                        on_text(event.delta.text)
                # parts_seq stays a few seconds behind the stream, so a reader
                # that resyncs mid-draft resumes inside the event buffer.
                em.checkpoint()
            if not ctx.cancelled():
                final = stream.get_final_message()
    except Exception as exc:
        if not ctx.cancelled():
            error = exc
    finally:
        ctx.active.detach(stream)
        delta.flush()

    step = f"sub{ctx.request}:{ctx.tool_use_id[-12:]}"
    cost = Decimal("0")
    if final is not None:
        totals = record_request_usage(
            ctx.run, final.usage, model=request["model"], role=f"subagent:{spec.key}", step=step,
            request_id=request_id, stop_reason=final.stop_reason or "",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        cost = totals.cost_usd
        if ctx.add_usage is not None:
            ctx.add_usage(totals)
    elif start_usage is not None:
        start_usage["output_tokens"] = max(int(start_usage.get("output_tokens") or 0),
                                           estimate_output_tokens(chars))
        totals = record_request_usage(
            ctx.run, start_usage, model=request["model"], role=f"subagent:{spec.key}", step=step,
            request_id=request_id, estimated=True,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        if ctx.add_usage is not None:
            ctx.add_usage(totals)

    if ctx.cancelled():
        fail("Stopped.", code="cancelled", artifact_id=artifact_id, status="cancelled")
        raise CancelRequested()
    if error is not None:
        if isinstance(error, GateStop | CancelRequested):
            raise error
        run_error = classify(error)
        raise fail(run_error.message, code=run_error.code, artifact_id=artifact_id)
    if final.stop_reason == "refusal":
        raise fail(f"The {spec.name.lower()} agent declined this request.", code="refused",
                   artifact_id=artifact_id)

    em.update_part(part_id, status="validating")
    raw = "".join(b.text for b in final.content if getattr(b, "type", "") == "text")
    extra = {}
    if spec.checks_links:
        extra["links"] = link_facts(ctx.website, content_blocks, extra_inputs)
    if spec.inputs is not None:
        extra["inputs"] = extra_inputs
    try:
        validated = spec.validate(raw, final.stop_reason, **extra)
    except ValueError as exc:
        message = str(exc)
        if final.stop_reason == "max_tokens" and max_tokens < limit:
            message += (f" The remaining AI allowance covered {max_tokens:,} of the "
                        f"{limit:,} output tokens this agent can use.")
        raise fail(message, code="invalid_output", artifact_id=artifact_id) from None
    if validated.data is not None:
        # Exactly the data the version saves (no ``drafting``).
        em.emit(EventType.ARTIFACT_SNAPSHOT, artifact_id=str(artifact_id), content=validated.data)
    elif spec.streams == "snapshot":
        em.emit(EventType.ARTIFACT_SNAPSHOT, artifact_id=str(artifact_id),
                content=json.loads(validated.content))

    version = _save_version(ctx, spec, artifact_id=artifact_id, existing=existing is not None,
                            title=title, validated=validated, cost=cost)
    em.emit(EventType.ARTIFACT_COMPLETED, artifact_id=str(artifact_id), version=version,
            title=title, summary=validated.summary, warnings=validated.warnings)
    em.update_part(part_id, status="done", artifact_id=str(artifact_id), version=version,
                   summary=validated.summary, error=None, progress=None)
    em.checkpoint(force=True)
    return ToolResult.ok({
        "artifact_id": str(artifact_id),
        "version": version,
        "kind": spec.kind,
        "title": title,
        "summary": validated.summary,
        "warnings": [w["message"] for w in validated.warnings][:10],
    }, summary=validated.summary)
