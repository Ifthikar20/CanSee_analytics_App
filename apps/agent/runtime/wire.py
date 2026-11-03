"""API wire messages: what the model saw, stored verbatim and replayed.

Every AgentMessage stores, in ``wire``, the wire messages it contributes
in order: a user node its user message (plus appended ``role: "system"``
context), an assistant node every assistant response and tool-result
message of its run. The history of a turn is the concatenation of the
wires on the path from the root to the turn's user message, so a branch
always replays its own prefix byte for byte.

``render`` turns that concatenation into a valid request without editing
anything a model produced: it answers tool calls a stopped run left
open, moves a system message that would be followed by a user message
(a turn that produced nothing) to after it, and joins consecutive
messages of the same role. It is a pure function of the stored wires, so
a later request on the same branch always extends an earlier one.
"""
from __future__ import annotations

import copy
import json
import re

from apps.agent.constants import MessageRole

STOPPED_RESULT = "Stopped before this ran."
# Text the SDK keeps on parsed blocks that is not part of the API shape.
_CLIENT_ONLY_KEYS = ("parsed_output",)
_DATE_RE = re.compile(r"Today \(UTC\): (\d{4}-\d{2}-\d{2})")


# ── Storage ─────────────────────────────────────────────────────────────


def loads(text: str | None) -> list[dict]:
    if not text:
        return []
    value = json.loads(text)
    return value if isinstance(value, list) else []


def dumps(messages: list[dict]) -> str:
    """JSON text for AgentMessage.wire. json escapes NUL as \\u0000, so the
    TEXT column never holds a raw NUL byte."""
    return json.dumps(messages, ensure_ascii=False, separators=(",", ":"))


# ── Building messages ───────────────────────────────────────────────────


def text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def user_message(text: str) -> dict:
    return {"role": "user", "content": [text_block(text)]}


def system_message(text: str) -> dict:
    return {"role": "system", "content": [text_block(text)]}


def assistant_message(blocks: list[dict]) -> dict:
    return {"role": "assistant", "content": blocks}


def block_to_wire(block) -> dict:
    """A response content block as a request block (SDK object or dict)."""
    data = block.to_dict(mode="json") if hasattr(block, "to_dict") else copy.deepcopy(dict(block))
    for key in _CLIENT_ONLY_KEYS:
        data.pop(key, None)
    return data


def tool_result(tool_use_id: str, content: str, *, is_error: bool = False) -> dict:
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if is_error:
        block["is_error"] = True
    return block


def stopped_results(tool_use_ids: list[str]) -> dict:
    """The user message that answers tool calls that never ran."""
    return {
        "role": "user",
        "content": [tool_result(tid, STOPPED_RESULT, is_error=True) for tid in tool_use_ids],
    }


def tool_use_ids(blocks: list[dict]) -> list[str]:
    return [b["id"] for b in blocks if b.get("type") == "tool_use" and b.get("id")]


# ── Response content filters ────────────────────────────────────────────


def _result_ids(blocks: list[dict]) -> set[str]:
    return {
        b.get("tool_use_id")
        for b in blocks
        if str(b.get("type", "")).endswith("_tool_result") and b.get("tool_use_id")
    }


def fallback_filter(blocks: list[dict]) -> tuple[list[dict], list[str]]:
    """Blocks to keep after a server-side fallback, and the tool_use ids dropped.

    Before the final ``fallback`` block the next model cannot use thinking,
    redacted thinking, tool calls or a server tool call without its
    result, so none of them is echoed back (and no dropped tool runs).
    """
    last = None
    for i, b in enumerate(blocks):
        if b.get("type") == "fallback":
            last = i
    if last is None:
        return list(blocks), []
    paired = _result_ids(blocks[:last])
    kept: list[dict] = []
    dropped: list[str] = []
    for i, b in enumerate(blocks):
        kind = b.get("type")
        if i < last:
            if kind in ("thinking", "redacted_thinking"):
                continue
            if kind == "tool_use":
                dropped.append(b.get("id"))
                continue
            if kind == "server_tool_use" and b.get("id") not in paired:
                continue
        kept.append(b)
    return kept, dropped


def safe_prefix(blocks: list[dict], completed: set[int]) -> list[dict]:
    """What a stopped response keeps: blocks up to the first unsafe one.

    Text is kept even when cut off. A thinking block needs its signature,
    a tool call must be complete, and a server tool call needs its result
    in the response. Trailing thinking is dropped (allowed), so the kept
    message ends with content the next request can follow.
    """
    paired = _result_ids(blocks)
    kept: list[dict] = []
    for i, b in enumerate(blocks):
        kind = b.get("type")
        done = i in completed
        if kind == "text":
            if (b.get("text") or "").strip():
                kept.append(b)
            if not done:
                break
            continue
        if kind == "thinking":
            if done and b.get("signature"):
                kept.append(b)
                continue
            break
        if kind == "server_tool_use":
            if done and b.get("id") in paired:
                kept.append(b)
                continue
            break
        if not done:
            break
        kept.append(b)
    while kept and kept[-1].get("type") in ("thinking", "redacted_thinking"):
        kept.pop()
    kept, _ = fallback_filter(kept)
    return kept


# ── History ─────────────────────────────────────────────────────────────


def path_to(message) -> list:
    """The messages from the chat's root to ``message``, inclusive."""
    from apps.agent.models import AgentMessage

    nodes = {
        m.id: m
        for m in AgentMessage.objects.filter(chat_id=message.chat_id).only(
            "id", "parent_id", "role", "wire", "depth", "status", "chat_id",
        )
    }
    nodes[message.id] = nodes.get(message.id, message)
    path = []
    current = nodes.get(message.id)
    seen = set()
    while current is not None and current.id not in seen:
        seen.add(current.id)
        path.append(current)
        current = nodes.get(current.parent_id) if current.parent_id else None
    path.reverse()
    return path


def history_of(nodes) -> list[dict]:
    """The stored wire messages of ``nodes``, concatenated in order."""
    out: list[dict] = []
    for node in nodes:
        out.extend(loads(node.wire))
    return out


def last_context_date(messages: list[dict]) -> str | None:
    """The newest "Today (UTC): YYYY-MM-DD" a system message carried."""
    found = None
    for m in messages:
        if m.get("role") != "system":
            continue
        for block in _content(m):
            if block.get("type") == "text":
                for match in _DATE_RE.finditer(block.get("text") or ""):
                    found = match.group(1)
    return found


def _content(message: dict) -> list[dict]:
    content = message.get("content")
    if isinstance(content, str):
        return [text_block(content)] if content else []
    return list(content or [])


def _normalized(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        out.append({**m, "content": _content(m)})
    return out


def _answer_open_tool_calls(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    for i, m in enumerate(messages):
        out.append(m)
        if m.get("role") != MessageRole.ASSISTANT:
            continue
        ids = tool_use_ids(m["content"])
        if not ids:
            continue
        nxt = messages[i + 1] if i + 1 < len(messages) else None
        answered = set()
        if nxt is not None and nxt.get("role") == MessageRole.USER:
            answered = {
                b.get("tool_use_id") for b in nxt["content"] if b.get("type") == "tool_result"
            }
        # A request never ends on open tool calls: the runner answers them
        # first. Stored wire does only after a crash; answer those here.
        missing = [tid for tid in ids if tid not in answered]
        if missing:
            out.append(stopped_results(missing))
    return out


def _place_system_messages(messages: list[dict]) -> list[dict]:
    """A system message must be last or followed by an assistant message;
    one followed by user messages moves after them."""
    out: list[dict] = []
    pending: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            pending.append(m)
            continue
        if role == MessageRole.ASSISTANT and pending:
            out.extend(pending)
            pending = []
        out.append(m)
    out.extend(pending)
    return out


def _merge_same_role(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        if out and out[-1].get("role") == m.get("role"):
            merged = dict(out[-1])
            merged["content"] = list(out[-1]["content"]) + list(m["content"])
            out[-1] = merged
        else:
            out.append(dict(m))
    return out


def render(messages: list[dict]) -> list[dict]:
    """The request ``messages`` for stored wire messages (see module doc)."""
    msgs = _normalized(copy.deepcopy(messages))
    msgs = _answer_open_tool_calls(msgs)
    msgs = _place_system_messages(msgs)
    return _merge_same_role(msgs)


def outside_text(messages: list[dict]) -> str:
    """Everything that came from outside the model: the user's words, tool
    results and server tool results (search hits, fetched pages)."""
    chunks = []
    for m in messages:
        for block in _content(m):
            kind = str(block.get("type") or "")
            if m.get("role") == MessageRole.USER and kind == "text":
                chunks.append(block.get("text") or "")
            elif m.get("role") == MessageRole.USER and kind == "tool_result":
                content = block.get("content")
                chunks.append(content if isinstance(content, str)
                              else json.dumps(content, ensure_ascii=False))
            elif kind.endswith("_tool_result"):
                chunks.append(json.dumps(block, ensure_ascii=False))
    return "\n".join(chunks)


def flatten(messages: list[dict]) -> list[tuple[str, str]]:
    """(role, canonical block JSON) pairs: the order the model reads.

    Two requests share a prefix exactly when one's flattened list starts
    with the other's.
    """
    out = []
    for m in messages:
        for block in _content(m):
            out.append((m.get("role"), json.dumps(block, sort_keys=True, ensure_ascii=False)))
    return out
