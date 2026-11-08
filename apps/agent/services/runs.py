"""Starting runs: send, regenerate, edit, continue and re-run.

Each writes the new tree nodes and a queued run in one transaction,
under a per-user advisory lock (one active run per user, one per chat),
then dispatches the run. A send with an Idempotency-Key the user already
used returns that earlier run instead of starting another.

Tree rules: regenerate adds an assistant sibling under the same user
message; edit adds a user sibling (and its reply) under the same parent;
continue adds a synthetic "Continue" user message under a truncated
reply; a research re-run adds a generated user message ("Re-run all 21
research agents") under the chat's current leaf, and its run makes no
model request. The chat's current leaf moves to the new reply.

A send or an edit may name a report template (apps.agent.report_templates):
its outline goes into the user message's system note, and the reply keeps
the key in ``research["template"]`` so saving it as a report labels it.
Regenerate, edit and continue keep the template of the reply they replace
or extend unless an edit names another one.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.agent import report_templates
from apps.agent.constants import (
    CHAT_PREVIEW_MAX_LENGTH,
    MAX_SWARM_VERSIONS,
    AgentKey,
    MessageRole,
    MessageStatus,
    RunStatus,
    RunTrigger,
    TitleSource,
    VersionStatus,
)
from apps.agent.exceptions import InsufficientAllowance, RunInProgress, VersionLimit
from apps.agent.prompts import context as context_prompt
from apps.agent.runtime import wire
from apps.agent.runtime.dispatch import dispatch_run
from apps.agent.runtime.title import fallback_title
from apps.agent.services import quota
from core.exceptions import CanseeException, ResourceNotFound

MAX_MESSAGE_CHARS = 20_000
MAX_IDEMPOTENCY_KEY = 64
CONTINUE_TEXT = "Continue"
_AGENT_RE = re.compile(
    r"(?:started with|selected the) [^()\n]*\((" + "|".join(AgentKey.values) + r")\)"
)
# The first line of prompts.context.report_template.
_TEMPLATE_RE = re.compile(
    r"^For this message the user chose the [^()\n]* report template \(("
    + "|".join(report_templates.KEYS) + r")\)\.", re.MULTILINE,
)


class InvalidRequest(CanseeException):
    def __init__(self, message: str):
        super().__init__(message, code="invalid", status_code=400)


class TemplateUnavailable(CanseeException):
    """400 ``template_unavailable``: a known template this chat or this
    workspace cannot run."""

    def __init__(self, message: str):
        super().__init__(message, code="template_unavailable", status_code=400)


@dataclass
class StartedRun:
    user_message: object | None
    assistant_message: object
    run: object
    # True when an Idempotency-Key matched an earlier request.
    replayed: bool = False
    # What a starter adds to its 202 response (a re-run: {"swarm_version"}).
    extra: dict | None = None


def clean_text(text) -> str:
    if not isinstance(text, str) or not text.strip():
        raise InvalidRequest("Write a message first.")
    text = text.replace("\x00", "").strip()
    if len(text) > MAX_MESSAGE_CHARS:
        raise InvalidRequest(f"Messages can be at most {MAX_MESSAGE_CHARS} characters.")
    return text


def clean_agent(agent) -> str | None:
    if agent in (None, ""):
        return None
    if agent not in AgentKey.values:
        raise InvalidRequest("Unknown agent.")
    return agent


def clean_template(template) -> str | None:
    """A report template key, or None for no template (400 when unknown)."""
    if template in (None, ""):
        return None
    if report_templates.get(template) is None:
        raise InvalidRequest("Unknown report template.")
    return template


def check_template(chat, key: str | None) -> None:
    """Refuse a template the chat's tools or the workspace cannot serve."""
    from apps.agent.services.catalog import template_unavailable

    template = report_templates.get(key)
    if template is None:
        return
    reason = template_unavailable(template)
    if reason is not None:
        raise TemplateUnavailable(reason["message"])
    if template.required_tools:
        try:
            offered = {t.get("name") for t in json.loads(chat.snapshot.tools_json or "[]")}
        except (TypeError, ValueError):
            offered = set()
        if not set(template.required_tools) <= offered:
            raise TemplateUnavailable(
                f"The {template.name} template needs a newer chat. Start a new chat to use it.")


def _wire_template(message) -> str | None:
    """The template named in a user message's system note, if any."""
    if message is None or not message.wire:
        return None
    found = None
    for m in wire.loads(message.wire):
        if m.get("role") != "system":
            continue
        for block in m.get("content") or []:
            for match in _TEMPLATE_RE.finditer(block.get("text") or ""):
                found = match.group(1)
    return found


def reply_template(reply) -> str | None:
    """The template a reply was written with: its research key, else the
    one named in its user message's note."""
    research = reply.research if isinstance(reply.research, dict) else {}
    key = research.get("template")
    if report_templates.get(key) is not None:
        return key
    return _wire_template(reply.parent) if reply.parent_id else None


def clean_idempotency_key(key) -> str | None:
    key = (key or "").strip()
    if not key:
        return None
    if len(key) > MAX_IDEMPOTENCY_KEY:
        raise InvalidRequest(f"Idempotency-Key can be at most {MAX_IDEMPOTENCY_KEY} characters.")
    return key


def _last_agent(history: list[dict], default: str) -> str:
    found = default
    for m in history:
        if m.get("role") != "system":
            continue
        for block in m.get("content") or []:
            for match in _AGENT_RE.finditer(block.get("text") or ""):
                found = match.group(1)
    return found


def _tracked_prompt_count(website) -> int:
    from apps.prompt_library.models import BrandPrompt

    return BrandPrompt.objects.filter(website=website, is_archived=False).count()


def user_wire(chat, parent, text: str, *, agent: str | None = None,
              template: str | None = None) -> list[dict]:
    """The wire a new user message contributes: the message, then the
    system context it needs (project context first; later a date update
    when the date changed, and the agent the user picked), then the
    outline of the report template the user picked."""
    today = timezone.now().date()
    messages = [wire.user_message(text)]
    notes = []
    if parent is None:
        notes.append(context_prompt.project_context(
            chat.website, entry_agent=chat.entry_agent,
            tracked_prompts=_tracked_prompt_count(chat.website), today=today,
        ))
        if agent and agent != chat.entry_agent:
            notes.append(context_prompt.agent_selected(agent))
    else:
        history = wire.history_of(wire.path_to(parent))
        if wire.last_context_date(history) != today.isoformat():
            notes.append(context_prompt.date_update(today))
        if agent and agent != _last_agent(history, chat.entry_agent):
            notes.append(context_prompt.agent_selected(agent))
    chosen = report_templates.get(template)
    if chosen is not None:
        notes.append(context_prompt.report_template(chosen))
    if notes:
        messages.append(wire.system_message("\n\n".join(notes)))
    return messages


def _create_turn(chat, *, user, parent, text: str | None, trigger: str,
                 user_message=None, agent: str | None = None,
                 idempotency_key: str | None = None, template: str | None = None,
                 note_template: bool = True,
                 constraints: dict | None = None) -> StartedRun:
    """Nodes and run for one turn. ``user_message`` reuses an existing user
    node (regenerate); otherwise a new one is created from ``text``.
    ``template`` is recorded on the reply; a new user node also carries its
    outline unless ``note_template`` is False (continue: the note is
    already in the history)."""
    from apps.agent.models import AgentChat, AgentMessage, AgentRun

    now = timezone.now()
    if user_message is None:
        user_message = AgentMessage.objects.create(
            chat=chat, parent=parent, role=MessageRole.USER,
            depth=(parent.depth + 1) if parent else 0,
            wire=wire.dumps(user_wire(chat, parent, text, agent=agent,
                                      template=template if note_template else None)),
            parts=[{"id": "p1", "type": "text", "text": text}],
            status=MessageStatus.COMPLETE, started_at=now, finished_at=now,
        )
        created_user = user_message
    else:
        created_user = None
    model = (chat.snapshot.config or {}).get("model", "")
    assistant = AgentMessage.objects.create(
        chat=chat, parent=user_message, role=MessageRole.ASSISTANT,
        depth=user_message.depth + 1, status=MessageStatus.STREAMING, model=model,
        research={"template": template} if template else {},
    )
    try:
        with transaction.atomic():
            run = AgentRun.objects.create(
                chat=chat, user=user, website=chat.website, user_message=user_message,
                assistant_message=assistant, trigger=trigger, status=RunStatus.QUEUED,
                idempotency_key=idempotency_key, constraints=constraints or {},
            )
    except IntegrityError:
        raise RunInProgress() from None
    fields = {"current_leaf": assistant, "last_message_at": now}
    if created_user is not None and trigger != RunTrigger.CONTINUE:
        fields["preview"] = text[:CHAT_PREVIEW_MAX_LENGTH]
        if parent is None and not chat.title and chat.title_source == TitleSource.AUTO:
            fields["title"] = fallback_title(text)
    AgentChat.objects.filter(id=chat.id).update(**fields)
    for key, value in fields.items():
        setattr(chat, key, value)
    return StartedRun(user_message=created_user, assistant_message=assistant, run=run)


def _replay(user, chat, key: str | None) -> StartedRun | None:
    from apps.agent.models import AgentRun

    if not key:
        return None
    run = (AgentRun.objects.select_related("user_message", "assistant_message")
           .filter(user=user, idempotency_key=key).first())
    if run is None:
        return None
    if run.chat_id != chat.id:
        raise InvalidRequest("This Idempotency-Key was already used for another chat.")
    return StartedRun(user_message=run.user_message, assistant_message=run.assistant_message,
                      run=run, replayed=True)


def _start(user, build) -> StartedRun:
    """Run ``build`` under the user's lock, then dispatch the run it created."""
    with transaction.atomic():
        quota.lock_user(user)
        started = build()
    if not started.replayed:
        dispatch_run(started.run.id)
        for obj in (started.run, started.assistant_message, started.user_message):
            if obj is not None:
                obj.refresh_from_db()
    return started


def _locked_checks(user, chat) -> None:
    """Allowance, send rate and concurrency, in the order the API reports them."""
    quota.check_allowance(user)
    quota.check_send_rate(user)
    quota.check_chat_free(chat)
    quota.check_user_slots(user)


def _load_chat(chat):
    from apps.agent.models import AgentChat

    return AgentChat.objects.select_related("website", "snapshot").get(id=chat.id)


def clean_engines(engines) -> list[str] | None:
    """The engines a send limits its run to: a list of public engine keys
    (chatgpt, claude, gemini, grok, perplexity), duplicates dropped. None or
    an empty list sets no limit; anything else is 400 invalid."""
    from apps.agent.tools.schemas import ENGINE_KEYS

    if engines in (None, []):
        return None
    if not isinstance(engines, list) or not all(isinstance(e, str) for e in engines):
        raise InvalidRequest("engines must be a list of engine keys.")
    unknown = [e for e in engines if e not in ENGINE_KEYS]
    if unknown:
        raise InvalidRequest(
            f"Unknown engine: {unknown[0]}. Use {', '.join(ENGINE_KEYS)}.")
    return list(dict.fromkeys(engines))


def start_send(*, user, chat, text, parent_id=None, agent=None, idempotency_key=None,
               template=None, engines=None) -> StartedRun:
    """A user message and its reply. ``template`` is a report template key
    applied to this reply; ``engines`` limits the AI engines the reply's
    research asks (AgentRun.constraints["engines"])."""
    from apps.agent.models import AgentChat, AgentMessage

    text = clean_text(text)
    agent = clean_agent(agent)
    template = clean_template(template)
    engines = clean_engines(engines)
    constraints = {"engines": engines} if engines else None
    key = clean_idempotency_key(idempotency_key)
    quota.check_configured()

    def build() -> StartedRun:
        current = _load_chat(chat)
        replay = _replay(user, current, key)
        if replay is not None:
            return replay
        if parent_id:
            parent = AgentMessage.objects.filter(id=parent_id, chat=current).first()
            if parent is None:
                raise ResourceNotFound("Message not found.")
        else:
            parent = current.current_leaf
        if parent is not None and parent.role != MessageRole.ASSISTANT:
            raise InvalidRequest("A message can only follow a reply.")
        check_template(current, template)
        _locked_checks(user, current)
        if parent is None and agent and agent != current.entry_agent:
            AgentChat.objects.filter(id=current.id).update(entry_agent=agent)
            current.entry_agent = agent
        return _create_turn(current, user=user, parent=parent, text=text, trigger=RunTrigger.SEND,
                            agent=agent, idempotency_key=key, template=template,
                            constraints=constraints)

    return _start(user, build)


def _message_in(user, website, message_id, *, role: str):
    from apps.agent.models import AgentMessage

    message = (AgentMessage.objects.select_related("chat", "chat__website", "chat__snapshot")
               .filter(id=message_id, chat__website=website, chat__user=user).first())
    if message is None or message.role != role:
        raise ResourceNotFound("Message not found.")
    return message


def start_regenerate(*, user, website, message_id) -> StartedRun:
    message = _message_in(user, website, message_id, role=MessageRole.ASSISTANT)
    quota.check_configured()
    # The user message is reused, so its note (and the outline) stays.
    template = reply_template(message)

    def build() -> StartedRun:
        chat = _load_chat(message.chat)
        _locked_checks(user, chat)
        return _create_turn(chat, user=user, parent=None, text=None,
                            trigger=RunTrigger.REGENERATE, user_message=message.parent,
                            template=template)

    return _start(user, build)


_INHERIT = object()


def start_edit(*, user, website, message_id, text, template=_INHERIT) -> StartedRun:
    """``template`` left out keeps the edited message's template; None or
    "" sends the edit without one."""
    message = _message_in(user, website, message_id, role=MessageRole.USER)
    text = clean_text(text)
    template = _wire_template(message) if template is _INHERIT else clean_template(template)
    quota.check_configured()

    def build() -> StartedRun:
        chat = _load_chat(message.chat)
        check_template(chat, template)
        _locked_checks(user, chat)
        return _create_turn(chat, user=user, parent=message.parent, text=text,
                            trigger=RunTrigger.EDIT, template=template)

    return _start(user, build)


def start_continue(*, user, website, message_id) -> StartedRun:
    message = _message_in(user, website, message_id, role=MessageRole.ASSISTANT)
    if message.status != MessageStatus.TRUNCATED:
        raise InvalidRequest("Only a reply that was cut off can be continued.")
    quota.check_configured()
    template = reply_template(message)

    def build() -> StartedRun:
        chat = _load_chat(message.chat)
        _locked_checks(user, chat)
        return _create_turn(chat, user=user, parent=message, text=CONTINUE_TEXT,
                            trigger=RunTrigger.CONTINUE, template=template,
                            note_template=False)

    return _start(user, build)


def _version_payload(version) -> dict:
    return {
        "swarm_id": str(version.swarm_id),
        "version": version.version,
        "scope": version.scope,
        "scope_key": version.scope_key,
        "task_ids": list(version.task_ids or []),
    }


def start_rerun(*, user, chat, swarm, scope: str, scope_key: str, task_ids: list[str],
                text: str, idempotency_key=None, need_usd=None) -> StartedRun:
    """A research re-run: a new turn at the end of the chat's current
    branch (a generated user message, an assistant message and a run with
    trigger ``rerun``, which makes no model request) and the swarm's next
    version, queued for that run. ``need_usd`` is the re-run's upper-bound
    cost, checked against the allowance left (403 insufficient_allowance).
    A replayed Idempotency-Key returns the earlier run and its version.
    """
    from apps.agent.models import AgentSwarm, AgentSwarmVersion
    from apps.agent.services.swarms import allowance_shortfall

    key = clean_idempotency_key(idempotency_key)
    quota.check_configured()

    def build() -> StartedRun:
        current = _load_chat(chat)
        replay = _replay(user, current, key)
        if replay is not None:
            version = AgentSwarmVersion.objects.filter(run=replay.run).first()
            if version is None:
                raise InvalidRequest("This Idempotency-Key was already used for another request.")
            replay.extra = {"swarm_version": _version_payload(version)}
            return replay
        parent = current.current_leaf
        if parent is None or parent.role != MessageRole.ASSISTANT:
            raise InvalidRequest("Send a message first.")
        locked = AgentSwarm.objects.select_for_update().get(id=swarm.id)
        number = (AgentSwarmVersion.objects.filter(swarm=locked)
                  .order_by("-version").values_list("version", flat=True).first() or 0) + 1
        if number > MAX_SWARM_VERSIONS:
            raise VersionLimit(MAX_SWARM_VERSIONS)
        _locked_checks(user, current)
        shortfall = allowance_shortfall(user, need_usd) if need_usd is not None else None
        if shortfall is not None:
            from apps.metering.services import prompt_runs

            if prompt_runs.is_limited(user, chat.website):
                # Free accounts never see dollars.
                raise CanseeException(prompt_runs.ALLOWANCE_SHORT_FREE,
                                      code="insufficient_allowance", status_code=403)
            raise InsufficientAllowance(**shortfall)
        started = _create_turn(current, user=user, parent=parent, text=text,
                               trigger=RunTrigger.RERUN, idempotency_key=key)
        version = AgentSwarmVersion.objects.create(
            swarm=locked, version=number, scope=scope, scope_key=scope_key or "",
            task_ids=list(task_ids), status=VersionStatus.QUEUED, run=started.run,
            message=started.assistant_message,
        )
        started.extra = {"swarm_version": _version_payload(version)}
        return started

    return _start(user, build)
