"""Real-API smoke test of the agent request path. Run it by hand:

    python manage.py agent_smoke

It sends the orchestrator's real system prompt and tools twice with a
one-line message (two small billed requests) and checks that the second
request reads the prompt cache (cache_read_input_tokens > 0). Nothing is
written to the database and no usage is recorded.
"""
from __future__ import annotations

import time

from django.core.management.base import BaseCommand, CommandError

from apps.agent.conf import agent_conf, api_key_configured
from apps.agent.llm.client import get_client
from apps.agent.llm.pricing import cost_for
from apps.agent.llm.request import build_turn_request
from apps.agent.llm.snapshot import canonical, snapshot_id, snapshot_payload

PROMPT = "This is a connectivity check. Reply with the single word: ready"


class Command(BaseCommand):
    help = "Send two real requests through the agent request path and check prompt caching."

    def add_arguments(self, parser):
        parser.add_argument("--max-tokens", type=int, default=2048)

    def handle(self, *args, **options):
        from apps.agent.models import AgentPromptSnapshot

        if not api_key_configured():
            raise CommandError("ANTHROPIC_API_KEY is not set; nothing was sent.")
        payload = snapshot_payload()
        snapshot = AgentPromptSnapshot(
            id=snapshot_id(payload), agent_key=payload["agent_key"], version=payload["version"],
            system_text=payload["system"], tools_json=canonical(payload["tools"]),
            config=payload["config"],
        )
        messages = [{"role": "user", "content": [{"type": "text", "text": PROMPT}]}]
        client = get_client()
        reads = []
        for attempt in (1, 2):
            request = build_turn_request(snapshot, messages, max_tokens=options["max_tokens"])
            started = time.monotonic()
            with client.beta.messages.stream(**request) as stream:
                final = stream.get_final_message()
            elapsed = time.monotonic() - started
            usage = final.usage
            text = "".join(b.text for b in final.content if getattr(b, "type", "") == "text")
            reads.append(usage.cache_read_input_tokens or 0)
            self.stdout.write(
                f"request {attempt}: model={final.model} stop={final.stop_reason} "
                f"input={usage.input_tokens} output={usage.output_tokens} "
                f"cache_write={usage.cache_creation_input_tokens or 0} "
                f"cache_read={usage.cache_read_input_tokens or 0} "
                f"cost=${cost_for(request['model'], usage, default_ttl=agent_conf().cache_ttl)} "
                f"time={elapsed:.1f}s reply={text.strip()[:40]!r}"
            )
        if reads[1] > 0:
            self.stdout.write(self.style.SUCCESS(f"cache_read > 0 on the second request ({reads[1]})"))
        else:
            raise CommandError("The second request did not read the prompt cache.")
