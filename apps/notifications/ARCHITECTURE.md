# Notifications App

## Purpose

Multi-channel notification system — in-app, email, Slack, and Discord. Handles real-time alerts (hot leads, keyword changes), scheduled digests (daily/weekly reports), inbound chat commands (Slack events/slash commands, Discord interactions), and user-configurable notification preferences.

## Architecture

```
Event Source (any app)
  │
  ├── In-app: Notification.objects.create(...)
  │
  ├── Email: SendGrid / Django send_mail
  │
  └── External Integrations:
        IntegrationConnection (Slack/Discord)
          ├── Webhook URL (encrypted)
          ├── External team id (Slack team_id / Discord guild_id)
          ├── Schedule (realtime / daily / weekly)
          ├── Format (summary / detailed)
          └── Event filters (daily_report, hot_leads, trend_digest, milestones)

Inbound chat commands (api/v1/chat_webhooks.py)
  ├── POST /api/v1/notifications/discord/interactions/  (Ed25519-verified)
  ├── POST /api/v1/notifications/slack/events/          (HMAC-verified)
  └── POST /api/v1/notifications/slack/commands/        (HMAC-verified)
        └── answer_chat_command Celery task ("ai" queue)
              report / security / ask / scan / help
```

## Models

| Model | Purpose |
|---|---|
| `Notification` | In-app notification with type, title, message, data (JSON), read status, and action URL for deep linking. |
| `NotificationPreference` | Per-user channel preferences: hot lead email/Slack, weekly report, competitor changes, audit complete. Includes encrypted Slack webhook URL. |
| `IntegrationConnection` | External platform connection (Slack, Discord). Stores encrypted webhook URL, channel name, external team/channel ids for inbound command resolution, schedule config, message format, and per-type notification toggles. |

## Notification Types

| Type | Channels | Trigger |
|---|---|---|
| Hot lead detected | In-app, email, Slack, Discord | Lead score crosses threshold |
| Keyword rank change | In-app, email | Keyword alert fires |
| Competitor change | In-app | Weekly crawl detects change |
| Audit completed | In-app | LLM ranking audit finishes |
| Weekly report | Email, Slack/Discord | Monday 9 AM Celery beat |
| Voice agent callback | In-app, email | Callback reminder due |

## Celery Tasks

| Task | Schedule | Purpose |
|---|---|---|
| `send_weekly_reports` | Monday 9 AM | Generate and send weekly analytics digests |
| `send_daily_growth_reports` | Daily 9 AM | Growth/GEO/security digest per IntegrationConnection (weekly connections on Mondays only) |
| `answer_chat_command` | On demand | Execute an inbound Slack/Discord command and deliver the reply |

## Key Design Decisions

- **Encrypted webhook URLs** — `EncryptedTextField` for Slack/Discord webhooks, protecting credentials at rest.
- **Signature-verified inbound endpoints** — Discord interactions use Ed25519 over `timestamp + body`; Slack uses HMAC-SHA256 over `v0:{ts}:{body}` with replay rejection, mirroring the Stripe webhook hardening.
- **User-controlled frequency** — Users choose per-integration whether to receive messages in real-time, daily, or weekly.
- **Message format options** — Summary (condensed) or detailed (full data) to match different team workflows.
- **Unique per platform** — `unique_together = [("user", "platform")]` ensures one connection per platform per user.

## Dependencies

- **Depends on:** `accounts`, `core` (encryption), and lazily on `websites`, `analytics`, `llm_ranking`, `brand_vault`, `agents` for report/command data
- **Depended on by:** `analytics` (keyword alerts), `billing` (trial ending)
