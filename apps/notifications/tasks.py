import logging
from datetime import date, datetime, timedelta

import requests
from celery import shared_task

logger = logging.getLogger("apps")

# Platform hard limits for chat replies.
DISCORD_CONTENT_LIMIT = 2000
DISCORD_EMBED_DESCRIPTION_LIMIT = 4000
SLACK_TEXT_LIMIT = 3000

# Hard bound on the ask command's fact block (numbers + content sections) so
# prompt cost stays predictable however large the account's prompt library
# or audit history grows. The content section is truncated first; the
# numbers section is never sacrificed to fit content.
ASK_FACT_BLOCK_CHAR_CAP = 4000
ASK_FACT_BLOCK_MORE_LINE = "...more in the dashboard."

CHAT_COMMANDS = ("report", "growth", "security", "usage", "ask", "scan", "help")

# Replies when the ask command cannot produce an AI answer. Said plainly,
# never dressed up as an answer.
ASK_NOT_CONFIGURED_REPLY = "Cansee couldn't answer right now: AI isn't available."
ASK_FAILED_REPLY = (
    "Cansee couldn't answer right now: the AI request failed. "
    "Try again in a moment."
)


@shared_task(name="apps.notifications.tasks.send_weekly_reports")
def send_weekly_reports():
    """Email the report digest to users who keep the weekly report on.

    The body is the same deterministic digest the chat ``report`` command
    sends. Users without a website have nothing to report and get no mail.
    """
    from django.utils.html import escape

    from apps.accounts.models import User
    from apps.notifications.services.email_service import EmailService
    from apps.websites.models import Website

    users = (
        User.objects.filter(is_active=True)
        .exclude(notification_preferences__weekly_report=False)
        .filter(id__in=Website.objects.values("user_id"))
    )
    for user in users:
        try:
            body = _format_report_text(_build_report_data(user))
            html = "".join(
                "<p>" + escape(block).replace("\n", "<br>") + "</p>"
                for block in body.split("\n\n")
            )
            EmailService.send_email(
                to=user.email,
                subject="Your Cansee Weekly Report",
                html_content=html,
            )
        except Exception as e:
            logger.error(f"Weekly report failed for user {user.id}: {e}")


@shared_task(name="apps.notifications.tasks.send_daily_growth_reports")
def send_daily_growth_reports():
    """
    Send growth reports to all active IntegrationConnections.
    Queries analytics, GEO visibility, and brand security data, then
    formats per platform. Runs daily at 09:00 via Celery beat; "weekly"
    connections only receive it on Mondays and "realtime" connections
    never receive the digest.
    """
    from apps.notifications.models import IntegrationConnection

    connections = IntegrationConnection.objects.filter(
        is_active=True, notify_daily_report=True
    ).select_related("user")

    if not connections.exists():
        logger.info("No active integration connections for daily reports")
        return

    today = date.today()
    today_label = today.strftime("%A, %b %d")

    for conn in connections:
        if conn.frequency == "realtime":
            continue  # realtime connections get event alerts, not digests
        if conn.frequency == "weekly" and today.weekday() != 0:
            continue  # weekly digests go out on Mondays only

        try:
            # Gather data for this user's websites
            report = _build_report_data(conn.user)

            # Claude narration is reserved for "detailed" connections: those
            # teams opted into the long-form digest, so they get the
            # conversational summary. "summary" connections keep the compact
            # deterministic template (fast, zero AI spend). When narration is
            # unavailable (no key, allowance exhausted, provider error) the
            # digest degrades to the template layout - it never fails.
            narrative = None
            if conn.message_format == "detailed":
                narrative = _narrated_report_body(conn, report)

            if conn.platform == "slack":
                _send_slack_report(conn.webhook_url, report, today_label,
                                   narrative=narrative)
            elif conn.platform == "discord":
                _send_discord_report(conn.webhook_url, report, today_label,
                                     narrative=narrative)
            elif conn.platform == "teams":
                _send_teams_report(conn.webhook_url, report, today_label,
                                   narrative=narrative)
            else:
                logger.warning(f"Unknown report platform skipped: {conn.platform}")
                continue

            logger.info(f"Daily report sent to {conn.platform} for user {conn.user_id}")

        except Exception as e:
            logger.error(f"Daily report failed for user {conn.user_id} ({conn.platform}): {e}")


def _build_report_data(user) -> dict:
    """Gather analytics, GEO visibility, and brand security data for a user.

    Sections that have no data are left empty/flagged so the renderers can
    say so honestly instead of printing fake zeros.
    """
    from django.utils import timezone

    from apps.websites.services.website_service import WebsiteService

    websites = list(WebsiteService.created_by_qs(user).order_by("created_at")[:3])
    data = {
        "visitors_24h": 0,
        "pageviews_24h": 0,
        # Percent change vs the previous 24h across all reported websites;
        # None when the previous 24h had no visitors to compare against.
        "visitors_change": None,
        "top_page": None,
        "has_analytics": False,
        "geo": [],
        "security": {"open_total": 0, "high": 0, "medium": 0, "low": 0, "top_action": ""},
    }

    now = timezone.now()
    visitors_prev = 0
    for website in websites:
        try:
            # Analytics overview
            from apps.analytics.services.analytics_service import AnalyticsService
            overview = AnalyticsService.get_overview(
                website_id=str(website.id), period="24h"
            )
            previous = AnalyticsService.get_overview(
                website_id=str(website.id), period="custom",
                start_date=(now - timedelta(hours=48)).isoformat(),
                end_date=(now - timedelta(hours=24)).isoformat(),
            )
            if isinstance(overview, dict):
                data["has_analytics"] = True
                data["visitors_24h"] += overview.get("total_visitors", 0) or 0
                data["pageviews_24h"] += overview.get("total_pageviews", 0) or 0
                if isinstance(previous, dict):
                    visitors_prev += previous.get("total_visitors", 0) or 0
                if not data["top_page"]:
                    top_pages = AnalyticsService.get_top_pages(
                        website_id=str(website.id), period="24h"
                    )
                    if top_pages:
                        first = top_pages[0]
                        data["top_page"] = (
                            first.get("url") or first.get("page") or "/"
                        )
        except Exception:
            pass

        try:
            # GEO visibility (12-month overview per website)
            from apps.llm_ranking.services.visibility_series import (
                build_overview_for_website,
            )
            geo = build_overview_for_website(user, website)
            if geo.get("has_data"):
                data["geo"].append({
                    "website": website.name,
                    "visibility_pct": geo.get("brand_current"),
                    "delta_pct": geo.get("brand_delta_pct"),
                })
        except Exception:
            pass

    if visitors_prev:
        data["visitors_change"] = round(
            (data["visitors_24h"] - visitors_prev) / visitors_prev * 100, 1,
        )

    try:
        data["security"] = _security_overview(user)
    except Exception:
        pass

    return data


def _visibility_text(value) -> str:
    return "not measured" if value is None else f"{value}%"


def _trend_text(value) -> str:
    return "no trend yet" if value is None else f"{value:+.1f}% trend"


def _security_overview(user, website=None) -> dict:
    """Open SafetyAlert counts by severity plus the top recommended action.

    ``website`` narrows the counts to one project; without it the whole
    account is summarized (the digest email's account-level view).
    """
    from apps.brand_vault.models import SafetyAlert
    from apps.brand_vault.services.security.detectors import (
        DETECTOR_INDEX,
        ISSUE_FALLBACK,
    )
    from apps.websites.services.website_service import WebsiteService

    open_alerts = SafetyAlert.objects.filter(
        website__in=WebsiteService.created_by_qs(user), status=SafetyAlert.STATUS_OPEN,
    ).only("severity", "detector_code", "issue")
    if website is not None:
        open_alerts = open_alerts.filter(website=website)

    counts = {"high": 0, "medium": 0, "low": 0}
    rank = {"high": 0, "medium": 1, "low": 2}
    top_alert = None
    for alert in open_alerts:
        counts[alert.severity] = counts.get(alert.severity, 0) + 1
        if top_alert is None or rank.get(alert.severity, 3) < rank.get(top_alert.severity, 3):
            top_alert = alert

    top_action = ""
    if top_alert is not None:
        code = top_alert.detector_code or ISSUE_FALLBACK.get(top_alert.issue, "")
        detector = DETECTOR_INDEX.get(code)
        if detector:
            top_action = detector.recommended_action

    return {
        "open_total": counts["high"] + counts["medium"] + counts["low"],
        "high": counts["high"],
        "medium": counts["medium"],
        "low": counts["low"],
        "top_action": top_action,
    }


def _report_section_lines(data: dict) -> dict:
    """Shared plain-text section lines for the report renderers."""
    if data.get("has_analytics"):
        change = data.get("visitors_change")
        change_str = (
            "no visitors the day before to compare" if change is None
            else f"{change:+}% from yesterday"
        )
        analytics = (
            f"{data['visitors_24h']:,} visitors ({change_str}), "
            f"{data['pageviews_24h']:,} pageviews"
        )
        if data.get("top_page"):
            analytics += f". Top page: {data['top_page']}"
    else:
        analytics = "No analytics data recorded in the last 24 hours."

    if data.get("geo"):
        geo_lines = [
            f"{row['website']}: {_visibility_text(row['visibility_pct'])} AI visibility "
            f"({_trend_text(row['delta_pct'])})"
            for row in data["geo"]
        ]
        geo = "\n".join(geo_lines)
    else:
        geo = "No AI visibility data yet - run a visibility scan to start tracking."

    sec = data.get("security") or {}
    if sec.get("open_total"):
        security = (
            f"{sec['open_total']} open alerts "
            f"({sec['high']} high, {sec['medium']} medium, {sec['low']} low)."
        )
        if sec.get("top_action"):
            security += f"\nRecommended action: {sec['top_action']}"
    else:
        security = "No open brand-security alerts."

    return {"analytics": analytics, "geo": geo, "security": security}


def _dashboard_url() -> str:
    """Base URL of the Cansee dashboard, from settings (no trailing slash)."""
    from django.conf import settings

    url = getattr(settings, "FRONTEND_URL", "") or "http://localhost:5173"
    return url.rstrip("/")


def _format_report_text(data: dict) -> str:
    """Plain-text digest body used for chat-command replies and the weekly email."""
    sections = _report_section_lines(data)
    return (
        f"Traffic (last 24h): {sections['analytics']}\n\n"
        f"AI visibility:\n{sections['geo']}\n\n"
        f"Brand security: {sections['security']}\n\n"
        f"View the full dashboard at {_dashboard_url()}"
    )


# System prompt for the Claude narration of report digests. The narrator only
# rephrases facts computed by _build_report_data; it must never add numbers.
REPORT_NARRATION_SYSTEM_PROMPT = (
    "You are Cansee, reporting to the team's channel. Write a short, "
    "conversational growth summary (5 to 9 sentences maximum) STRICTLY from "
    "the facts provided in the user message. Never invent, estimate, or "
    "extrapolate numbers, names, or trends that are not in those facts. Lead "
    "with what changed or grew, then what needs attention, then close with "
    "one concrete next step. Plain text only: no markdown headers, no bullet "
    "points, no emojis."
)


def _report_facts(data: dict) -> str:
    """Serialize the deterministic report data into plain facts for narration."""
    lines = []

    if data.get("has_analytics"):
        change = data.get("visitors_change")
        change_str = (
            "no visitors the day before to compare" if change is None
            else f"{change:+}% vs the day before"
        )
        line = (
            f"Traffic (last 24h): {data.get('visitors_24h', 0):,} visitors "
            f"({change_str}), "
            f"{data.get('pageviews_24h', 0):,} pageviews."
        )
        if data.get("top_page"):
            line += f" Top page: {data['top_page']}"
        lines.append(line)
    else:
        lines.append("Traffic (last 24h): no analytics data recorded.")

    if data.get("geo"):
        for row in data["geo"]:
            if row["visibility_pct"] is None:
                lines.append(f"AI visibility for {row['website']}: not measured yet.")
                continue
            lines.append(
                f"AI visibility for {row['website']}: {row['visibility_pct']}% of AI "
                f"answers mention the brand ({_trend_text(row['delta_pct'])})."
            )
    else:
        lines.append("AI visibility: no completed visibility scans yet.")

    sec = data.get("security") or {}
    if sec.get("open_total"):
        line = (
            f"Brand security: {sec['open_total']} open alerts "
            f"({sec.get('high', 0)} high, {sec.get('medium', 0)} medium, "
            f"{sec.get('low', 0)} low)."
        )
        if sec.get("top_action"):
            line += f" Top recommended action: {sec['top_action']}"
        lines.append(line)
    else:
        lines.append("Brand security: no open alerts.")

    return "\n".join(lines)


def _narrated_report_body(connection, data: dict) -> str | None:
    """Claude-narrated digest body, or None when narration is unavailable.

    The facts stay deterministic (_build_report_data); Claude only rephrases
    them conversationally and is instructed never to add numbers. Every
    failure mode - missing API key, exhausted monthly allowance, provider
    error, empty completion - returns None so callers fall back to the
    deterministic template instead of failing or inventing data.
    """
    try:
        from core.llm import ClaudeUtility

        result = ClaudeUtility(model="claude-haiku-4-5", max_tokens=500).query(
            _report_facts(data),
            system_prompt=REPORT_NARRATION_SYSTEM_PROMPT,
            user=connection.user,
            website=_resolve_website(connection.user),
            role="chat_report_narration",
            module="notifications",
        )
    except Exception as exc:
        logger.warning(f"Report narration failed; using template: {exc}")
        return None
    if not getattr(result, "succeeded", False):
        return None
    return (result.text or "").strip() or None


def _send_slack_report(webhook_url: str, data: dict, today: str,
                       narrative: str | None = None):
    """Format and send a Slack block kit message.

    With ``narrative`` (detailed connections, Claude narration succeeded) the
    body is the conversational summary; otherwise the sectioned template.
    """
    from apps.notifications.services.slack_service import SlackService

    header = {
        "type": "header",
        "text": {"type": "plain_text", "text": f"Daily Growth Report - {today}"},
    }
    footer = {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": f"<{_dashboard_url()}|View full dashboard>"}],
    }

    if narrative:
        blocks = [
            header,
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": narrative[:SLACK_TEXT_LIMIT]},
            },
            {"type": "divider"},
            footer,
        ]
    else:
        sections = _report_section_lines(data)
        blocks = [
            header,
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Traffic*\n{sections['analytics']}"},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*AI visibility*\n{sections['geo']}"},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Brand security*\n{sections['security']}"},
            },
            {"type": "divider"},
            footer,
        ]

    SlackService.send_message(
        webhook_url=webhook_url,
        text=f"Daily Growth Report - {today}",
        blocks=blocks,
    )


def _send_discord_report(webhook_url: str, data: dict, today: str,
                         narrative: str | None = None):
    """Format and send a Discord embed message.

    With ``narrative`` (detailed connections, Claude narration succeeded) the
    embed carries the conversational summary; otherwise the field template.
    """
    from apps.notifications.services.discord_service import DiscordService

    footer = f"View the full dashboard at {_dashboard_url()}"

    if narrative:
        DiscordService.send_message(
            webhook_url=webhook_url,
            title=f"Daily Growth Report - {today}",
            description=narrative[:DISCORD_EMBED_DESCRIPTION_LIMIT],
            footer=footer,
        )
        return

    sections = _report_section_lines(data)
    fields = [
        {"name": "Traffic", "value": sections["analytics"][:1024], "inline": False},
        {"name": "AI visibility", "value": sections["geo"][:1024], "inline": False},
        {"name": "Brand security", "value": sections["security"][:1024], "inline": False},
    ]

    DiscordService.send_message(
        webhook_url=webhook_url,
        title=f"Daily Growth Report - {today}",
        fields=fields,
        footer=footer,
    )


def _send_teams_report(webhook_url: str, data: dict, today: str,
                       narrative: str | None = None):
    """Format and send a Microsoft Teams report card.

    Teams cards render one text body (markdown), so we assemble the same
    sections the Slack/Discord reports use into a single block — the
    narrative when a detailed connection has one, else the template.
    """
    from apps.notifications.services.teams_service import TeamsService

    footer = f"View the full dashboard at {_dashboard_url()}"

    if narrative:
        body = f"{narrative}\n\n{footer}"
    else:
        sections = _report_section_lines(data)
        body = (
            f"**Traffic**\n{sections['analytics']}\n\n"
            f"**AI visibility**\n{sections['geo']}\n\n"
            f"**Brand security**\n{sections['security']}\n\n"
            f"{footer}"
        )

    TeamsService.send_message(
        webhook_url=webhook_url,
        title=f"Daily Growth Report - {today}",
        text=body,
    )


# ── Inbound chat commands (Slack slash/mention + Discord slash) ───────────────


@shared_task(
    name="apps.notifications.tasks.answer_chat_command",
    bind=True,
    max_retries=1,
    default_retry_delay=5,
)
def answer_chat_command(self, connection_id: str, command: str, text: str,
                        respond_to: dict, invoker: str = ""):
    """Execute one chat command and deliver the answer back to the platform.

    ``respond_to`` describes the reply route:
      {"kind": "discord_followup", "interaction_token": ...}  (15-min token;
      the max_retries=1/short-countdown pairing keeps retries inside it)
      {"kind": "slack_response_url", "url": ...}
      {"kind": "slack_channel", "channel": ..., "thread_ts": ...}
    """
    from apps.notifications.models import IntegrationConnection

    connection = (
        IntegrationConnection.objects.filter(id=connection_id)
        .select_related("user")
        .first()
    )
    if connection is None:
        logger.warning(f"Chat command for unknown connection {connection_id}")
        return

    thread_ref = (
        (respond_to or {}).get("thread_ts")
        or (respond_to or {}).get("interaction_id")
        or ""
    )

    try:
        title, reply = _run_chat_command(connection, command, text, thread_ref)
    except Exception as exc:
        logger.error(f"Chat command '{command}' failed for {connection_id}: {exc}")
        try:
            self.retry(exc=exc)
            return
        except Exception:
            title, reply = "", (
                "Something went wrong answering that command. Please try again."
            )

    _deliver_chat_reply(respond_to, title=title, text=reply)


def _run_chat_command(connection, command: str, text: str,
                      thread_ref: str = "") -> tuple[str, str]:
    """Route one command. Returns (title, plain-text reply)."""
    command = (command or "").strip().lower() or "help"
    user = connection.user

    if command == "report":
        data = _build_report_data(user)
        today_label = date.today().strftime("%A, %b %d")
        narration = _narrated_report_body(connection, data)
        if narration:
            body = f"{narration}\n\nView the full dashboard at {_dashboard_url()}"
        else:
            body = _format_report_text(data)
        return f"Daily Growth Report - {today_label}", body

    if command == "growth":
        return "Growth Movers", _growth_command_text(user)

    if command == "security":
        return "Brand Security", _security_command_text(user)

    if command == "usage":
        return "AI Usage", _usage_command_text(user)

    if command == "ask":
        return "", _answer_question(connection, (text or "").strip(), thread_ref)

    if command == "scan":
        return "", _queue_scan_command(user)

    return "", _help_text()


def _help_text() -> str:
    return (
        "Cansee commands:\n"
        "report - Send your latest growth, AI visibility, and security digest.\n"
        "growth - Show which websites and prompts grew or slipped, and where to focus.\n"
        "security - List open brand-security alerts with recommended fixes.\n"
        "usage - Show this month's AI token usage and allowance status.\n"
        "ask <question> - Ask Cansee about your growth or visibility data.\n"
        "scan - Queue a fresh AI visibility scan of your saved prompts."
    )


def _security_command_text(user) -> str:
    """Open brand-security alerts, ordered by severity, with mitigations."""
    from apps.brand_vault.models import SafetyAlert
    from apps.brand_vault.services.security.detectors import (
        DETECTOR_INDEX,
        ISSUE_FALLBACK,
    )
    from apps.websites.services.website_service import WebsiteService

    overview = _security_overview(user)
    if not overview["open_total"]:
        return (
            "No open brand-security alerts for your websites. If you have not "
            "scanned recently, run a brand-security scan from the Brand "
            "Security page to check the latest AI answers."
        )

    rank = {"high": 0, "medium": 1, "low": 2}
    alerts = sorted(
        SafetyAlert.objects.filter(
            website__in=WebsiteService.created_by_qs(user), status=SafetyAlert.STATUS_OPEN,
        ).select_related("website"),
        key=lambda a: (rank.get(a.severity, 3), -a.last_seen_at.timestamp()),
    )[:5]

    lines = [
        f"Brand security health: {overview['open_total']} open alerts "
        f"({overview['high']} high, {overview['medium']} medium, "
        f"{overview['low']} low). Top {len(alerts)} by severity:"
    ]
    for alert in alerts:
        code = alert.detector_code or ISSUE_FALLBACK.get(alert.issue, "")
        detector = DETECTOR_INDEX.get(code)
        action = (
            detector.recommended_action if detector
            else "Review this alert in the Brand Security dashboard."
        )
        label = alert.title or alert.get_issue_display()
        lines.append(
            f"- [{alert.severity.upper()}] {label} ({alert.website.name})\n"
            f"  Fix: {action}"
        )
    return "\n".join(lines)


def _usage_command_text(user) -> str:
    """Billing-period AI usage/allowance status.

    Same service as the Settings page's /auth/me/ai-usage endpoint
    (apps.metering.services.usage_reader), so the chat command and the
    UI always agree. The allowance is the plan's USD spend cap — the
    honest number enforcement uses — with tokens reported as real
    counts, never a projected "token capacity".
    """
    from apps.metering.services.usage_reader import get_period_usage

    usage = get_period_usage(user)
    allowance = usage.get("allowance") or {}
    totals = usage.get("totals") or {}
    used_tokens = int(totals.get("total_tokens") or 0)
    requests = int(totals.get("calls") or 0)

    if not used_tokens and not requests:
        return (
            "No AI usage recorded this billing period yet. Usage appears "
            "here once you run a visibility scan (scan), ask questions "
            "(ask), or use the AI content tools in the dashboard."
        )

    lines = [
        f"This billing period: {used_tokens:,} tokens across "
        f"{requests:,} AI request{'s' if requests != 1 else ''}."
    ]

    resets_label = ""
    resets_at = allowance.get("resets_at") or ""
    if resets_at:
        try:
            resets_label = datetime.fromisoformat(resets_at).strftime("%b %d")
        except ValueError:
            resets_label = ""
    cap_usd = float(allowance.get("cap_usd") or 0)
    if cap_usd > 0:
        spent = float(allowance.get("spent_usd") or 0)
        pct = allowance.get("pct_used")
        line = f"Allowance: ${spent:.2f} of ${cap_usd:.2f} used ({pct}%)."
        if resets_label:
            line += f" Resets on {resets_label}."
        lines.append(line)
    else:
        lines.append("Allowance: no AI allowance is configured for this account.")

    top_modules = sorted(
        usage.get("by_module") or [],
        key=lambda m: -float(m.get("cost") or 0),
    )[:2]
    if top_modules:
        parts = [
            f"{m.get('module', 'unknown')} (${float(m.get('cost') or 0):.2f})"
            for m in top_modules
        ]
        lines.append(f"Top modules by spend this period: {', '.join(parts)}.")

    return "\n".join(lines)


def _truncate(text: str, limit: int = 80) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _prompt_extremes(prompts: list[dict], n: int) -> tuple[list[dict], list[dict]]:
    """Both ends of the Overview builder's weakest-first prompt rows.

    Returns (strongest, weakest) without listing the same prompt twice
    when there are fewer than 2n rows. Shared by the growth command and
    the ask fact pack so both slice the same product the same way.
    """
    weakest = prompts[:n]
    strongest = [p for p in reversed(prompts) if p not in weakest][:n]
    return strongest, weakest


def _growth_command_text(user) -> str:
    """Which websites and prompts grew, what slipped, and one suggested focus.

    Reuses the dashboard's read-only builders - the per-website Visibility
    Overview (brand + competitor deltas) and the Overview tab's per-prompt
    rows (already weakest-first) - rather than re-deriving metrics. Honest
    empty state when the account has no completed audits.
    """
    from apps.llm_ranking.services.overview_stats import build_overview_for_user
    from apps.llm_ranking.services.visibility_series import build_overview_for_website
    from apps.websites.services.website_service import WebsiteService

    site_rows = []
    for website in WebsiteService.created_by_qs(user).order_by("created_at")[:5]:
        try:
            overview = build_overview_for_website(user, website)
        except Exception:
            continue
        if not overview.get("has_data"):
            continue
        site_rows.append({
            "name": website.name,
            "current": overview.get("brand_current"),
            "delta": overview.get("brand_delta_pct"),
            "competitor_current": overview.get("competitor_current"),
            "competitor_delta": overview.get("competitor_delta_pct"),
        })

    prompts = []
    try:
        prompt_overview = build_overview_for_user(user)
        if prompt_overview.get("has_data"):
            prompts = prompt_overview.get("prompts") or []
    except Exception:
        prompts = []

    if not site_rows and not prompts:
        return (
            "No completed prompt runs yet, so there is no growth to "
            "report. Run scan to queue an AI visibility scan of your saved "
            "prompts - movers will show up here once it completes."
        )

    with_trend = [r for r in site_rows if r["delta"] is not None]
    up = sorted((r for r in with_trend if r["delta"] > 0), key=lambda r: -r["delta"])
    down = sorted((r for r in with_trend if r["delta"] < 0), key=lambda r: r["delta"])
    flat = [r for r in with_trend if r["delta"] == 0]
    no_trend = [r for r in site_rows if r["delta"] is None]

    def _site_line(row: dict) -> str:
        return (
            f"- {row['name']}: {_visibility_text(row['current'])} AI visibility "
            f"({_trend_text(row['delta'])}; competitor avg "
            f"{_visibility_text(row['competitor_current'])}, "
            f"{_trend_text(row['competitor_delta'])})"
        )

    blocks: list[str] = []
    if up:
        blocks.append("Moving up:\n" + "\n".join(_site_line(r) for r in up))
    if down:
        blocks.append("Slipping:\n" + "\n".join(_site_line(r) for r in down))
    if flat:
        blocks.append("Holding steady:\n" + "\n".join(_site_line(r) for r in flat))
    if no_trend:
        blocks.append(
            "Not enough runs for a trend yet:\n"
            + "\n".join(_site_line(r) for r in no_trend)
        )

    # The overview builder returns prompts weakest-first; pull both ends.
    strongest, weakest = _prompt_extremes(prompts, 3)
    if strongest:
        blocks.append("Strongest prompts:\n" + "\n".join(
            f'- "{_truncate(p["text"])}" - {p["visibility"]}% visibility'
            for p in strongest
        ))
    if weakest:
        blocks.append("Needs work:\n" + "\n".join(
            f'- "{_truncate(p["text"])}" - {p["visibility"]}% visibility'
            for p in weakest
        ))

    if weakest:
        worst = weakest[0]
        blocks.append(
            f'Suggested focus: improve coverage for "{_truncate(worst["text"])}" - '
            f"you currently appear in {worst['visibility']}% of AI answers for it."
        )
    elif down:
        blocks.append(
            f"Suggested focus: {down[0]['name']} is trending down "
            f"({down[0]['delta']:+.1f}%) - review its weakest prompts and "
            "refresh the content behind them."
        )
    elif up:
        blocks.append(
            "Suggested focus: keep the momentum - run scan again after your "
            "next content update to confirm the trend."
        )

    return "\n\n".join(blocks)


# Reply when every project the user created is one their current org role
# can only view: chat must refuse what the app refuses with a 403.
VIEW_ONLY_COMMAND_REPLY = (
    "Your organization role can view this project but not change it, so "
    "Cansee can't run scans or answer questions for it from chat. Ask an "
    "organization admin for member access."
)


def _resolve_website(user, *, min_role: str | None = None):
    """The user's first active created website they can still reach.

    With ``min_role``, only a website where their current role meets it
    (scan and ask create audits and spend engine runs, so they need
    "member", as in the app); the read-only digests pass none.
    """
    from apps.websites.services.website_service import WebsiteService

    qs = (
        WebsiteService.created_by_qs_with_role(user, min_role)
        if min_role else WebsiteService.created_by_qs(user)
    )
    return qs.filter(is_active=True).order_by("created_at").first()


def _no_writable_website_reply(user) -> str:
    """Why a write command found no project: none at all, or view-only."""
    if _resolve_website(user) is not None:
        return VIEW_ONLY_COMMAND_REPLY
    return "No active website found on your account. Add a website in Cansee first."


# ── Live fact pack for the ask command ────────────────────────────────────────
# A compact, deterministic block of current metrics injected into every ask
# prompt so free-form questions ("how much has traffic grown since
# yesterday?") can be answered from real data. Built entirely from local
# queries - never an LLM call - and each section degrades to omission when
# its source fails, so the block can never invent data.


def _traffic_fact_lines(user, website=None) -> list[str]:
    """Today-vs-yesterday traffic per website, with the derived delta.

    Extends the daily report builder's AnalyticsService usage with an
    explicit previous-24h window so both absolute numbers are stated and the
    delta is computed from them rather than guessed. With ``website`` the
    lines cover only that project; without it, the first three websites
    (the account-level digest view).
    """
    from django.utils import timezone

    from apps.analytics.services.analytics_service import AnalyticsService
    from apps.websites.services.website_service import WebsiteService

    now = timezone.now()
    lines = []
    if website is not None:
        targets = [website]
    else:
        targets = WebsiteService.created_by_qs(user).order_by("created_at")[:3]
    for website in targets:
        try:
            today = AnalyticsService.get_overview(
                website_id=str(website.id), period="24h",
            )
            yesterday = AnalyticsService.get_overview(
                website_id=str(website.id), period="custom",
                start_date=(now - timedelta(hours=48)).isoformat(),
                end_date=(now - timedelta(hours=24)).isoformat(),
            )
        except Exception:
            continue
        if not isinstance(today, dict) or not isinstance(yesterday, dict):
            continue
        t_visitors = today.get("total_visitors", 0) or 0
        y_visitors = yesterday.get("total_visitors", 0) or 0
        if y_visitors:
            delta = f"{(t_visitors - y_visitors) / y_visitors * 100:+.1f}% vs yesterday"
        elif t_visitors:
            delta = "no visitors yesterday, so no percentage change is computable"
        else:
            delta = "no visitors in either window"
        lines.append(
            f"- Traffic for {website.name}: last 24h {t_visitors} visitors / "
            f"{today.get('total_pageviews', 0) or 0} pageviews; previous 24h "
            f"(yesterday) {y_visitors} visitors / "
            f"{yesterday.get('total_pageviews', 0) or 0} pageviews; "
            f"visitor change {delta}."
        )
    return lines


def _visibility_fact_lines(user, website=None) -> list[str]:
    from apps.llm_ranking.services.visibility_series import build_overview_for_website
    from apps.websites.services.website_service import WebsiteService

    lines = []
    if website is not None:
        targets = [website]
    else:
        targets = WebsiteService.created_by_qs(user).order_by("created_at")[:3]
    for website in targets:
        try:
            overview = build_overview_for_website(user, website)
        except Exception:
            continue
        if not overview.get("has_data"):
            continue
        current = overview.get("brand_current")
        if current is None:
            lines.append(f"- AI visibility for {website.name}: not measured yet.")
            continue
        lines.append(
            f"- AI visibility for {website.name}: {current}% of AI answers "
            f"mention the brand ({_trend_text(overview.get('brand_delta_pct'))}; "
            f"competitor avg {_visibility_text(overview.get('competitor_current'))})."
        )
    if not lines:
        lines.append(
            "- AI visibility: no completed visibility scans yet (the scan "
            "command queues one)."
        )
    return lines


def _security_fact_lines(user, website=None) -> list[str]:
    overview = _security_overview(user, website=website)
    if not overview.get("open_total"):
        return ["- Brand security: no open alerts."]
    line = (
        f"- Brand security: {overview['open_total']} open alerts "
        f"({overview['high']} high, {overview['medium']} medium, "
        f"{overview['low']} low)."
    )
    if overview.get("top_action"):
        line += f" Top recommended action: {overview['top_action']}"
    return [line]


def _usage_fact_lines(user) -> list[str]:
    from apps.metering.services.usage_reader import get_period_usage

    usage = get_period_usage(user)
    allowance = (usage or {}).get("allowance") or {}
    used = int(allowance.get("used_tokens") or 0)
    cap_usd = float(allowance.get("cap_usd") or 0)
    line = f"- AI usage this billing period: {used:,} tokens"
    if cap_usd > 0:
        spent = float(allowance.get("spent_usd") or 0)
        pct = allowance.get("pct_used")
        line += f" (${spent:.2f} of the ${cap_usd:.2f} allowance, {pct}%)"
    return [line + "."]


def _saved_prompt_fact_lines(website) -> list[str]:
    """The website's saved prompt library, newest first, capped at 10 rows.

    Lets content questions ("what was my most recent prompt?") resolve
    deterministically: the list is explicitly ordered and indexed, and the
    ask system prompts define "most recent prompt" as entry 1.
    """
    from apps.prompt_library.models import BrandPrompt

    rows = list(
        BrandPrompt.objects.filter(website=website)
        .select_related("prompt")
        .order_by("-created_at")[:10]
    )
    if not rows:
        return []
    lines = ["Saved prompts, most recent first:"]
    for index, row in enumerate(rows, start=1):
        line = (
            f'{index}. "{_truncate(row.prompt.text, 90)}" '
            f"({'active' if row.prompt.is_active else 'inactive'})"
        )
        tags = [t.strip() for t in (row.tags or []) if isinstance(t, str) and t.strip()]
        tag_label = ", ".join(tags)
        if tag_label and len(tag_label) <= 40:
            line += f" [tags: {tag_label}]"
        lines.append(line)
    return lines


def _audit_content_fact_lines(user, website) -> list[str]:
    """Content of the latest completed visibility audit, bounded per list.

    Reuses the Overview builder products the growth command already reads
    (per-prompt rows and the brand table) plus the citations app's domain
    cards - no aggregation is re-derived here. Each subsection is guarded
    so a failing source degrades to omission, never to invented content.
    """
    from apps.llm_ranking.models import LLMRankingAudit

    audit = (
        LLMRankingAudit.objects.filter(
            website=website, status=LLMRankingAudit.STATUS_COMPLETED,
        )
        .order_by("-completed_at")
        .first()
    )
    if audit is None:
        return []

    heading = "Latest completed visibility audit"
    if audit.completed_at:
        heading += f" (finished {audit.completed_at.strftime('%Y-%m-%d %H:%M UTC')})"
    lines = [heading + ":"]

    try:
        from apps.llm_ranking.services.overview_stats import build_overview_for_user

        # Scoped to this website — the strongest/weakest prompt lists and
        # competitor table must describe THIS project, not whichever
        # project audited most recently.
        overview = build_overview_for_user(user, website=website)
    except Exception:
        overview = {}
    if overview.get("has_data"):
        strongest, weakest = _prompt_extremes(overview.get("prompts") or [], 5)
        if strongest:
            lines.append("Strongest prompts by AI visibility:")
            lines.extend(
                f'- "{_truncate(p["text"], 90)}" - {p["visibility"]}% visibility'
                for p in strongest
            )
        if weakest:
            lines.append("Weakest prompts by AI visibility:")
            lines.extend(
                f'- "{_truncate(p["text"], 90)}" - {p["visibility"]}% visibility'
                for p in weakest
            )
        competitors = [
            b for b in overview.get("brands") or [] if not b.get("is_target")
        ][:5]
        if competitors:
            lines.append("Top competitor brands by AI visibility:")
            lines.extend(
                f"- {b['name']}: {b['visibility']}% visibility" for b in competitors
            )

    try:
        from apps.citations.services.overview_domains import build_domain_cards

        domain_rows, _ = build_domain_cards([audit.id])
    except Exception:
        domain_rows = []
    if domain_rows:
        lines.append("Top cited domains in this audit (share of retrieved sources):")
        lines.extend(
            f"- {d['domain']}: {d['retrieved']}% of retrievals ({d['type']})"
            for d in domain_rows[:5]
        )

    # Nothing materialised under the heading - omit the section entirely.
    return lines if len(lines) > 1 else []


def _live_fact_block(user, website=None) -> str:
    """Deterministic plain-text fact block prepended to ask prompts.

    Two parts, numbers always first: the metric lines (traffic, visibility,
    security, usage) and a bounded content section (saved prompts plus the
    latest completed audit's content) so content questions ("what was my
    most recent prompt?") resolve from real rows. Sections whose queries
    fail are skipped rather than filled with zeros; an entirely failed
    build returns "" and the ask flow proceeds without facts. The whole
    block is hard-capped at ASK_FACT_BLOCK_CHAR_CAP characters, truncating
    the content section first.
    """
    from django.utils import timezone

    if website is None:
        try:
            website = _resolve_website(user)
        except Exception:
            website = None

    lines: list[str] = []
    # Traffic/visibility/security narrow to the selected project; AI usage
    # stays account-level because the allowance itself is account-level.
    for build in (
        lambda u: _traffic_fact_lines(u, website=website),
        lambda u: _visibility_fact_lines(u, website=website),
        lambda u: _security_fact_lines(u, website=website),
        _usage_fact_lines,
    ):
        try:
            lines.extend(build(user))
        except Exception:
            continue

    content_lines: list[str] = []
    if website is not None:
        try:
            content_lines.extend(_saved_prompt_fact_lines(website))
        except Exception:
            pass
        try:
            content_lines.extend(_audit_content_fact_lines(user, website))
        except Exception:
            pass

    if not lines and not content_lines:
        return ""
    stamp = timezone.now().strftime("%Y-%m-%d %H:%M UTC")
    header = (
        f"Live account facts as of {stamp} (deterministic Cansee platform "
        "data - answer metric and account-content questions from these "
        "facts and any other provided context; if the question needs data "
        "not tracked here, say so plainly and point at the Cansee command "
        "or dashboard page that has it; never fabricate numbers):"
    )
    base = [header, *lines]
    if not content_lines:
        return "\n".join(base)[:ASK_FACT_BLOCK_CHAR_CAP]

    # Keep as many content lines as the cap allows; when any are dropped,
    # say so instead of ending mid-list as if the account had nothing more.
    for keep in range(len(content_lines), -1, -1):
        candidate_lines = base + content_lines[:keep]
        if keep < len(content_lines):
            candidate_lines.append(ASK_FACT_BLOCK_MORE_LINE)
        candidate = "\n".join(candidate_lines)
        if len(candidate) <= ASK_FACT_BLOCK_CHAR_CAP:
            return candidate
    return "\n".join(base)[:ASK_FACT_BLOCK_CHAR_CAP]


def _answer_question(connection, question: str, thread_ref: str = "") -> str:
    """Answer a free-form question with a one-shot synthesis, grounded in
    live account facts and the company brain when available. (The hired-
    agents routing was removed with the agents app, 2026-08-24.)"""
    user = connection.user
    if not question:
        return "Ask a question after the command, e.g.: ask How is my AI visibility trending?"

    website = _resolve_website(user, min_role="member")
    if website is None:
        return _no_writable_website_reply(user)

    # The answer is grounded in a deterministic fact pack so questions
    # about live metrics and account content reflect real rows.
    facts = _live_fact_block(user, website)

    from apps.llm_ranking.providers import get_provider, get_synthesis_provider

    provider = get_provider("claude") or get_synthesis_provider()
    if provider is None:
        return ASK_NOT_CONFIGURED_REPLY

    context = ""
    try:
        from apps.rag.services.retriever import retrieve_context_block

        context = retrieve_context_block(
            user=user, website=website, query=question,
            top_k=3, max_chars=1200,
        ) or ""
    except Exception:
        context = ""

    system_prompt = (
        "You are Cansee, a growth and AI-visibility assistant. Answer "
        "questions about the user's website traffic, AI search visibility, "
        "and brand security. Be concise, factual, and plain-text. "
        "Answer metric questions from the live account facts and retrieved "
        "context in the prompt. The facts also include the account's saved "
        "prompts (listed most recent first - 'my most recent prompt' means "
        "the first entry of that list) and content from the latest "
        "completed visibility audit. If the question needs data that is not "
        "present there, say plainly what is not tracked here and which "
        "Cansee command or dashboard page has it. Never fabricate numbers; "
        "if you don't have the data to answer, say so instead of guessing."
    )
    prompt = (
        (f"{facts}\n\n" if facts else "")
        + (f"{context}\n\n" if context else "")
        + f"The user asked: {question}\n\n"
        "Reply concisely and helpfully in plain text (no JSON)."
    )
    result = provider.query(
        prompt, system_prompt,
        user=user, website=website,
        audit_id=f"chat_command:{connection.id}",
        role="chat_command", module="notifications",
    )
    reply = (getattr(result, "text", "") or "").strip()
    if not getattr(result, "succeeded", False) or not reply:
        logger.warning(
            "Chat ask failed for connection %s: %s",
            connection.id, (getattr(result, "error", "") or "empty reply")[:200],
        )
        return ASK_FAILED_REPLY
    return reply


def _queue_scan_command(user) -> str:
    """Queue an AI visibility audit over the user's saved prompts."""
    website = _resolve_website(user, min_role="member")
    if website is None:
        return _no_writable_website_reply(user)

    from apps.llm_ranking.services.audit_factory import create_audit
    from apps.llm_ranking.services.audit_runner import (
        NoSavedPromptsError,
        gather_saved_prompts,
    )
    from apps.llm_ranking.services.scan_dispatch import dispatch_scan

    try:
        prompts = gather_saved_prompts(website, user)
    except NoSavedPromptsError:
        return (
            f"{website.name} has no saved prompts to scan. Save prompts on "
            "the Prompts page first, then run scan again."
        )

    from apps.llm_ranking.services.audit_factory import NoLLMProviderConfigured
    from core.exceptions import CanseeException

    try:
        audit = create_audit(
            website=website, user=user, prompts=prompts, prompt_source="library",
        )
    except NoLLMProviderConfigured:
        return "Cansee couldn't start a scan: AI engines aren't available yet."
    except CanseeException as exc:
        # Chat surfaces render text, not HTTP errors — relay the message
        # (e.g. the monthly prompt allowance being used up) as a reply.
        return exc.message
    dispatch_scan(str(audit.id))
    return (
        f"Visibility scan queued for {website.name} across {len(prompts)} "
        "saved prompts. Results will appear on your dashboard shortly."
    )


def _deliver_chat_reply(respond_to: dict, *, title: str, text: str) -> None:
    """Deliver a command reply through the route described by respond_to."""
    from django.conf import settings

    kind = (respond_to or {}).get("kind", "")
    text = (text or "").strip() or "No answer available."

    if kind == "discord_followup":
        from apps.notifications.services.discord_service import DiscordService

        if title:
            DiscordService.send_followup(
                application_id=getattr(settings, "DISCORD_APPLICATION_ID", ""),
                interaction_token=respond_to.get("interaction_token", ""),
                title=title[:250],
                description=text[:DISCORD_EMBED_DESCRIPTION_LIMIT],
            )
        else:
            DiscordService.send_followup(
                application_id=getattr(settings, "DISCORD_APPLICATION_ID", ""),
                interaction_token=respond_to.get("interaction_token", ""),
                content=text[:DISCORD_CONTENT_LIMIT],
            )
        return

    if kind == "slack_response_url":
        url = respond_to.get("url", "")
        if not url:
            logger.warning("Slack response_url reply skipped: missing url.")
            return
        body = f"{title}\n{text}" if title else text
        try:
            requests.post(
                url,
                json={"response_type": "in_channel", "text": body[:SLACK_TEXT_LIMIT]},
                timeout=10,
            )
        except Exception as e:
            logger.error(f"Slack response_url delivery failed: {e}")
        return

    if kind == "slack_channel":
        from apps.notifications.services.slack_service import SlackService

        body = f"{title}\n{text}" if title else text
        SlackService.post_message(
            channel=respond_to.get("channel", ""),
            text=body[:SLACK_TEXT_LIMIT],
            thread_ts=respond_to.get("thread_ts", ""),
        )
        return

    logger.warning(f"Unknown chat reply route: {kind}")
