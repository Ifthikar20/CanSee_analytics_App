"""
Register the /cansee slash command with Discord.

Global registration (default) can take up to an hour to propagate;
pass --guild <id> during development for instant guild-scoped commands.

Usage:
    python manage.py register_discord_commands
    python manage.py register_discord_commands --guild 123456789012345678
"""
import json

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

DISCORD_API_BASE = "https://discord.com/api/v10"

# Discord application-command option types.
SUB_COMMAND = 1
OPTION_STRING = 3

CANSEE_COMMAND = {
    "name": "cansee",
    "type": 1,  # CHAT_INPUT
    "description": "Cansee growth and AI-visibility assistant",
    # Guild-only: commands are tenant-bound through the server link, so a
    # DM invocation has no account to resolve against. The endpoint also
    # refuses DMs; this hides the command there in the first place.
    "dm_permission": False,
    "contexts": [0],  # 0 = GUILD
    "options": [
        {
            "type": SUB_COMMAND,
            "name": "report",
            "description": "Send your latest growth, AI visibility, and security digest",
        },
        {
            "type": SUB_COMMAND,
            "name": "growth",
            "description": "Show which websites and prompts grew or slipped, and where to focus",
        },
        {
            "type": SUB_COMMAND,
            "name": "security",
            "description": "List open brand-security alerts with recommended fixes",
        },
        {
            "type": SUB_COMMAND,
            "name": "usage",
            "description": "Show this month's AI token usage and allowance status",
        },
        {
            "type": SUB_COMMAND,
            "name": "ask",
            "description": "Ask Cansee about your growth or visibility data",
            "options": [
                {
                    "type": OPTION_STRING,
                    "name": "question",
                    "description": "What do you want to know?",
                    "required": True,
                },
            ],
        },
        {
            "type": SUB_COMMAND,
            "name": "scan",
            "description": "Queue a fresh AI visibility scan of your saved prompts",
        },
        {
            "type": SUB_COMMAND,
            "name": "link",
            "description": "Link this server to your Cansee account with a link code",
            "options": [
                {
                    "type": OPTION_STRING,
                    "name": "code",
                    "description": "The link code from Cansee Integrations settings",
                    "required": True,
                },
            ],
        },
        {
            "type": SUB_COMMAND,
            "name": "help",
            "description": "List available Cansee commands",
        },
    ],
}


class Command(BaseCommand):
    help = "Register the /cansee slash command with Discord (global or per-guild)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--guild",
            default="",
            help="Register guild-scoped commands for this guild id (instant propagation).",
        )

    def handle(self, *args, **options):
        application_id = getattr(settings, "DISCORD_APPLICATION_ID", "")
        bot_token = getattr(settings, "DISCORD_BOT_TOKEN", "")
        if not application_id or not bot_token:
            raise CommandError(
                "DISCORD_APPLICATION_ID and DISCORD_BOT_TOKEN must be set. "
                "Get them from the Discord Developer Portal: General Information "
                "(Application ID) and Bot (Token)."
            )

        guild_id = options["guild"]
        if guild_id:
            url = f"{DISCORD_API_BASE}/applications/{application_id}/guilds/{guild_id}/commands"
            scope = f"guild {guild_id}"
        else:
            url = f"{DISCORD_API_BASE}/applications/{application_id}/commands"
            scope = "global"

        # PUT overwrites the full command set for the scope, so removed
        # commands disappear instead of lingering.
        response = requests.put(
            url,
            json=[CANSEE_COMMAND],
            headers={"Authorization": f"Bot {bot_token}"},
            timeout=15,
        )

        self.stdout.write(f"Discord responded {response.status_code} ({scope} scope)")
        try:
            payload = response.json()
        except json.JSONDecodeError:
            payload = None

        if response.status_code not in (200, 201):
            raise CommandError(f"Registration failed: {payload or response.text[:500]}")

        for command in payload or []:
            subcommands = ", ".join(
                opt.get("name", "?") for opt in command.get("options", [])
            )
            self.stdout.write(
                f"Registered /{command.get('name')} (id {command.get('id')}) "
                f"with subcommands: {subcommands}"
            )
