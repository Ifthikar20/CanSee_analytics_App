"""Single-turn orchestration for the Ask Cansee assistant.

Grounds every answer in (1) the deterministic per-tenant fact block and
(2) RAG retrieval over the tenant's unified knowledge corpus, then makes
one metered, spend-walled LLM call. Tenant scope is carried by the
(user, website) pair the caller resolved through TenantScopedAPIView —
this module never reads an identifier off the request body.

When no answer can be produced, :func:`answer` raises
:class:`AssistantUnavailable` instead of returning placeholder text, so
callers never store or send a non-answer as if it were one.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("apps")

# Max conversation turns from the client folded into the prompt.
_MAX_HISTORY_TURNS = 8
# Guard the prompt against a runaway pasted history.
_MAX_HISTORY_CHARS = 6000

_SYSTEM_PROMPT = (
    "You are Cansee, the user's AI growth and visibility assistant. You "
    "can see their real account data across the whole product: traffic and "
    "analytics, AI-search visibility across ChatGPT/Claude/Gemini/"
    "Perplexity, per-prompt performance, the saved prompt library, Google "
    "Search Console queries, brand-security findings, citations and source "
    "influence, audit history, the knowledge base, and AI usage.\n\n"
    "Rules:\n"
    "- Answer from the LIVE ACCOUNT FACTS and the retrieved KNOWLEDGE BASE "
    "context in the prompt. These are the user's real data.\n"
    "- Never fabricate numbers. If the data needed to answer is not present, "
    "say plainly what is not tracked and point to the Cansee page that has "
    "it (Analytics, AI Visibility, Brand Security, Prompt Library, Billing).\n"
    "- Be concise and helpful. Format your reply in clean GitHub-flavored "
    "Markdown: short paragraphs, **bold** for key figures, bullet lists, and "
    "compact tables when comparing things. Do not wrap the whole reply in a "
    "code block.\n"
    "- Write in a friendly, direct voice. Lead with the answer, then the "
    "supporting detail.\n"
    "- When you compare quantities across several things (brands, prompts, "
    "providers, pages) or show a value over time, ALSO emit one chart so "
    "the shape is visible at a glance. Use a fenced block tagged `chart` "
    "containing only JSON, on its own lines:\n"
    "```chart\n"
    '{"type": "bar", "title": "AI visibility by brand", "unit": "%", '
    '"labels": ["You", "Brex", "PayPal"], '
    '"datasets": [{"label": "Visibility", "data": [6.3, 15.5, 15.5]}]}\n'
    "```\n"
    "  type is bar, line or doughnut. At most 8 labels and 3 datasets. Use "
    "only numbers that appear in the facts above -- never invent a series "
    "to make a chart. Put the chart beside the prose that explains it, "
    "never instead of it, and at most two charts per reply."
)


class AssistantUnavailable(Exception):
    """The assistant could not produce an answer; nothing should be saved.

    ``code`` is ``assistant_not_configured`` (no AI provider key) or
    ``assistant_failed`` (the provider call failed or returned no text).
    ``message`` is plain language suitable for the user.
    """

    NOT_CONFIGURED = "assistant_not_configured"
    FAILED = "assistant_failed"

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


NOT_CONFIGURED_MESSAGE = "Ask Cansee isn't available yet: no AI provider is configured."


def get_assistant_provider():
    """The provider that answers questions, or None when no key is set.

    Claude is preferred; the synthesis chain can substitute another
    configured provider.
    """
    from apps.llm_ranking.providers import get_provider, get_synthesis_provider

    return get_provider("claude") or get_synthesis_provider()


def _format_history(history) -> str:
    if not history:
        return ""
    turns = []
    for turn in list(history)[-_MAX_HISTORY_TURNS:]:
        role = (turn.get("role") or "").strip().lower()
        content = (turn.get("content") or "").strip()
        if not content or role not in ("user", "assistant"):
            continue
        speaker = "User" if role == "user" else "Cansee"
        turns.append(f"{speaker}: {content}")
    if not turns:
        return ""
    block = "Conversation so far:\n" + "\n".join(turns)
    return block[-_MAX_HISTORY_CHARS:]


def _failure_message(error: str) -> str:
    if (error or "").startswith("monthly_ai_allowance_exceeded"):
        return (
            "Ask Cansee can't answer right now: this account has used its "
            "monthly AI allowance."
        )
    return "Ask Cansee couldn't get an answer from the AI provider. Please try again."


def answer(*, user, website, question, history=None) -> dict:
    """Answer a free-form question for one (user, website).

    Returns ``{"answer", "grounded", "provider", "model", "knowledge_status"}``.
    ``grounded`` is True only when the prompt carried real tenant data:
    a product section with actual figures or rows, or retrieved
    knowledge-base passages. "No data yet" statements do not count.
    ``knowledge_status`` is "used", "no_match", "retrieval_failed", or the
    retriever's reason code (e.g. "embeddings_not_configured").

    Raises :class:`AssistantUnavailable` when no provider is configured or
    the provider call yields no answer, and ``ValueError`` for an empty
    question.
    """
    question = (question or "").strip()
    if not question:
        raise ValueError("question is required")

    provider = get_assistant_provider()
    if provider is None:
        raise AssistantUnavailable(AssistantUnavailable.NOT_CONFIGURED, NOT_CONFIGURED_MESSAGE)

    from apps.assistant.services.context_builder import build_fact_block

    # The question drives which product areas get loaded (prompt metrics,
    # Search Console, citations, audits, the knowledge base...).
    facts = build_fact_block(user, website, question=question)

    context = ""
    try:
        from apps.rag.services.retriever import format_context_block, retrieve

        hits = retrieve(user=user, website=website, query=question, top_k=5)
        context = format_context_block(hits, max_chars=3000)
        if context:
            knowledge_status = "used"
        else:
            knowledge_status = hits.reason or "no_match"
    except Exception:
        logger.warning("assistant retrieval failed for website %s", website.id, exc_info=True)
        knowledge_status = "retrieval_failed"

    convo = _format_history(history)
    prompt = (
        (f"{facts.text}\n\n" if facts.text else "")
        + (f"{context}\n\n" if context else "")
        + (f"{convo}\n\n" if convo else "")
        + f"User question: {question}\n\n"
        "Answer in Markdown."
    )

    result = provider.query(
        prompt, _SYSTEM_PROMPT,
        user=user, website=website,
        audit_id=f"assistant:{website.id}",
        role="assistant_chat", module="assistant",
    )
    if not getattr(result, "succeeded", False):
        error = getattr(result, "error", "") or ""
        logger.warning("assistant provider call failed for website %s: %s", website.id, error)
        raise AssistantUnavailable(AssistantUnavailable.FAILED, _failure_message(error))
    text = (getattr(result, "text", "") or "").strip()
    if not text:
        raise AssistantUnavailable(
            AssistantUnavailable.FAILED,
            "Ask Cansee couldn't get an answer from the AI provider. Please try again.",
        )

    return {
        "answer": text,
        # It says the answer was written WITH this tenant's data in front
        # of it; it does not verify that the answer used it correctly.
        "grounded": facts.grounded or bool(context),
        # Which model actually answered. `get_provider("claude")` is the
        # preference but the synthesis fallback can substitute another, so
        # this is read off the provider that ran rather than assumed.
        "provider": getattr(provider, "name", "") or "",
        "model": getattr(provider, "model", "") or "",
        "knowledge_status": knowledge_status,
    }
