"""Brand Pulse agent: gather per-website brand insights and push them out.

The agent assembles one digest per website from sources that already exist
(analytics overview, AI visibility overview, Brand Security alerts, Brand
Research scans, source influence snapshots), renders it per platform, and
delivers it to every active IntegrationConnection the website owner has --
falling back to an in-app notification when there is none. It can also
queue a Brand Research scan itself when the latest one has gone stale, and
push new high-severity security alerts immediately.

Every data source is individually guarded: a failing source becomes an
absent section, never a crashed digest. Delivery failures are logged and
never raised to callers (the response auditor calls into this module from
inside a scan).
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger("apps")

# Platform hard limits (mirrors apps/notifications/tasks.py).
SLACK_TEXT_LIMIT = 3000
DISCORD_FIELD_LIMIT = 1024
DISCORD_DESCRIPTION_LIMIT = 4000

# A traffic swing at or beyond this magnitude counts as signal on its own.
TRAFFIC_SWING_SIGNAL_PCT = 25
# Weekly heartbeat: even a quiet website gets a digest at least this often.
HEARTBEAT_DAYS = 7
# An own-brand mention below this sentiment is a watchout.
NEGATIVE_SENTIMENT_THRESHOLD = -0.15
# An audit older than this without a schedule earns an action line.
STALE_AUDIT_DAYS = 7
# Auto-scan gates.
FRESH_SCAN_DAYS = 5
SCAN_QUEUE_COOLDOWN_DAYS = 3

DIGEST_ACTION_URL = "/llm-ranking/{website_id}/brand-research"


def _clip(text, limit: int) -> str:
    """Hard-cap a string for a platform limit, ellipsized."""
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _get_pulse(website):
    from apps.brand_vault.models import BrandPulse

    pulse, _ = BrandPulse.objects.get_or_create(website=website)
    return pulse


# ── Section gatherers (each guarded by the caller) ───────────────────────────


def _traffic_section(website) -> dict | None:
    from apps.analytics.services.analytics_service import AnalyticsService

    overview = AnalyticsService.get_overview(website_id=str(website.id), period="7d")
    if not isinstance(overview, dict):
        return None
    return {
        "total_visitors": overview.get("total_visitors", 0),
        "total_pageviews": overview.get("total_pageviews", 0),
        # None when the prior week had no visitors: growth is unknown.
        "visitor_growth_pct": overview.get("visitor_growth_pct"),
    }


def _visibility_section(website) -> dict | None:
    from apps.llm_ranking.services.visibility_series import build_overview_for_website

    overview = build_overview_for_website(website.user, website)
    if not isinstance(overview, dict) or not overview.get("has_data"):
        return None
    if overview.get("brand_current") is None:
        return None
    return {
        "visibility_pct": overview["brand_current"],
        # None with fewer than two measured months.
        "delta_pct": overview.get("brand_delta_pct"),
    }


def _audit_freshness(website) -> dict:
    from apps.llm_ranking.models import LLMRankingAudit, LLMRankingSchedule

    latest = (
        LLMRankingAudit.objects.filter(
            website=website, status=LLMRankingAudit.STATUS_COMPLETED,
        )
        .order_by("-completed_at")
        .first()
    )
    age_days = None
    if latest is not None:
        stamp = latest.completed_at or latest.created_at
        if stamp:
            age_days = max(0, (timezone.now() - stamp).days)
    has_schedule = LLMRankingSchedule.objects.filter(
        website=website, is_enabled=True,
    ).exists()
    stale = age_days is None or age_days > STALE_AUDIT_DAYS
    return {"age_days": age_days, "has_schedule": has_schedule, "stale": stale}


def _security_section(website, watermark) -> dict:
    from apps.brand_vault.models import SafetyAlert
    from apps.notifications.tasks import _security_overview

    overview = _security_overview(website.user, website=website)

    # Recurrence bumps occurrence_count on the existing row, so "new since
    # the last digest" must key on first_seen_at, never detected_at.
    rank = {"high": 0, "medium": 1, "low": 2}
    new_alerts = sorted(
        SafetyAlert.objects.filter(website=website, first_seen_at__gt=watermark),
        key=lambda a: (rank.get(a.severity, 3), -a.first_seen_at.timestamp()),
    )
    return {
        "open": overview,
        "new_count": len(new_alerts),
        "new_top": [
            {
                "reference": a.reference,
                "severity": a.severity,
                "issue": a.issue,
                "title": a.title or a.get_issue_display(),
                "source_url": a.source_url,
            }
            for a in new_alerts[:3]
        ],
    }


def _research_section(website, watermark) -> dict | None:
    """Latest complete Brand Research scan: watchouts, outreach, coverage."""
    from apps.citations.models import SourceScan, SourceScanStatus
    from apps.citations.services.source_scan import (
        _matches_target,
        derive_opportunities,
    )

    scan = (
        SourceScan.objects.filter(
            website=website, status=SourceScanStatus.COMPLETE,
        )
        .order_by("-created_at")
        .first()
    )
    if scan is None:
        return None
    target = website.name or ""

    # Negative-sentiment sources: per-result brands rows are
    # [{"name", "sentiment", "mentions", "weight", "quotes"}] with no own-
    # brand flag, so match names against the website brand the same way the
    # aggregation rollup does.
    negatives = []
    for result in scan.results.filter(relevant=True):
        for brand in result.brands or []:
            if not isinstance(brand, dict):
                continue
            sentiment = brand.get("sentiment") or 0.0
            if sentiment >= NEGATIVE_SENTIMENT_THRESHOLD:
                continue
            if not _matches_target(brand.get("name", ""), target):
                continue
            negatives.append({
                "domain": result.domain,
                "url": result.url,
                "sentiment": round(sentiment, 2),
            })
            break  # one entry per source
    negatives.sort(key=lambda n: n["sentiment"])

    opportunities = derive_opportunities(scan)
    threads = [o for o in opportunities if o.get("kind") == "thread"][:3]
    questions = [o for o in opportunities if o.get("kind") == "question"][:2]

    # Engine coverage: of the engines that answered, how many named us.
    answers = list(scan.engine_answers.filter(status="ok"))
    covered = 0
    for answer in answers:
        if any(
            isinstance(b, dict) and _matches_target(b.get("name", ""), target)
            for b in answer.brands or []
        ):
            covered += 1

    return {
        "scan_id": str(scan.id),
        "query": scan.query,
        "completed_at": scan.completed_at.isoformat() if scan.completed_at else None,
        "new_since_watermark": bool(scan.completed_at and scan.completed_at > watermark),
        "negatives": negatives[:3],
        "threads": [
            {
                "url": t.get("url", ""),
                "domain": t.get("domain", ""),
                "title": t.get("serp_title", ""),
                "competitors": t.get("competitors", [])[:3],
                "reason": t.get("reason", ""),
            }
            for t in threads
        ],
        "questions": [
            {"question": q.get("serp_title", ""), "url": q.get("url", "")}
            for q in questions
        ],
        "engines_answered": len(answers),
        "engines_naming_brand": covered,
    }


def _influence_domains(website) -> list[dict]:
    """Competitor-leaning domains from the latest influence snapshot."""
    from apps.citations.models import SourceInfluenceSnapshot

    snapshot = (
        SourceInfluenceSnapshot.objects.filter(website=website)
        .order_by("-period_end", "-created_at")
        .first()
    )
    if snapshot is None:
        return []
    picked = []
    for row in snapshot.top_domains or []:
        if not isinstance(row, dict):
            continue
        if row.get("is_competitor") and not row.get("is_target"):
            picked.append({
                "domain": row.get("apex_domain", ""),
                "share": row.get("share", 0),
            })
        if len(picked) >= 3:
            break
    return picked


# ── Digest assembly ──────────────────────────────────────────────────────────


def build_digest(website) -> dict | None:
    """Assemble the Brand Pulse digest, or None when there is no signal.

    Each section is gathered independently; a failing source is dropped
    rather than crashing the digest. Returns None only when nothing new
    happened AND the weekly heartbeat is not yet due.
    """
    pulse = _get_pulse(website)
    now = timezone.now()
    watermark = pulse.last_digest_at or (now - timedelta(days=HEARTBEAT_DAYS))

    digest: dict = {
        "website_id": str(website.id),
        "website_name": website.name or website.url or "your website",
        "generated_at": now.isoformat(),
        "actions": [],
    }

    try:
        digest["traffic"] = _traffic_section(website)
    except Exception:
        digest["traffic"] = None
    digest["has_analytics"] = digest["traffic"] is not None

    try:
        digest["visibility"] = _visibility_section(website)
    except Exception:
        digest["visibility"] = None

    try:
        freshness = _audit_freshness(website)
    except Exception:
        freshness = {"age_days": None, "has_schedule": False, "stale": False}
    digest["audit_freshness"] = freshness
    if freshness.get("stale") and not freshness.get("has_schedule"):
        digest["actions"].append(
            "No fresh prompt run this week. Queue a visibility scan or "
            "enable a schedule so AI visibility stays current."
        )

    try:
        digest["security"] = _security_section(website, watermark)
    except Exception:
        digest["security"] = None

    try:
        digest["research"] = _research_section(website, watermark)
    except Exception:
        digest["research"] = None

    try:
        digest["influence_domains"] = _influence_domains(website)
    except Exception:
        digest["influence_domains"] = []

    # No-signal gate: skip the digest when nothing new happened, traffic is
    # flat, and a digest already went out within the heartbeat window.
    new_alerts = (digest.get("security") or {}).get("new_count", 0)
    new_scan = bool((digest.get("research") or {}).get("new_since_watermark"))
    growth = abs((digest.get("traffic") or {}).get("visitor_growth_pct", 0) or 0)
    heartbeat_fresh = bool(
        pulse.last_digest_at
        and pulse.last_digest_at > now - timedelta(days=HEARTBEAT_DAYS)
    )
    if (
        not new_alerts
        and not new_scan
        and growth < TRAFFIC_SWING_SIGNAL_PCT
        and heartbeat_fresh
    ):
        return None
    return digest


# ── Renderers ────────────────────────────────────────────────────────────────


def _section_lines(digest: dict) -> list[tuple[str, str]]:
    """(heading, body) pairs shared by every renderer, in digest order."""
    sections: list[tuple[str, str]] = []

    visibility = digest.get("visibility")
    if visibility is not None:
        delta = visibility["delta_pct"]
        trend = (
            f"{delta:+.1f}% trend" if delta is not None
            else "no earlier month to compare"
        )
        sections.append((
            "Visibility",
            f"{visibility['visibility_pct']}% of AI answers mention the brand "
            f"({trend}).",
        ))

    traffic = digest.get("traffic")
    if traffic is not None:
        growth = traffic["visitor_growth_pct"]
        change = (
            f"{'+' if growth >= 0 else ''}{growth}% vs the prior week"
            if growth is not None
            else "no visitors the prior week to compare"
        )
        sections.append((
            "Traffic",
            f"{traffic['total_visitors']:,} visitors and "
            f"{traffic['total_pageviews']:,} pageviews in the last 7 days "
            f"({change}).",
        ))

    security = digest.get("security") or {}
    if security:
        open_counts = security.get("open") or {}
        if security.get("new_count"):
            lines = [
                f"{security['new_count']} new alert"
                f"{'s' if security['new_count'] != 1 else ''} since the last digest:"
            ]
            for alert in security.get("new_top") or []:
                line = (
                    f"- {alert['reference']} [{alert['severity'].upper()}] "
                    f"{alert['issue']}: {alert['title']}"
                )
                if alert.get("source_url"):
                    line += f" ({alert['source_url']})"
                lines.append(line)
            if open_counts.get("open_total"):
                lines.append(
                    f"Open in total: {open_counts['open_total']} "
                    f"({open_counts.get('high', 0)} high, "
                    f"{open_counts.get('medium', 0)} medium, "
                    f"{open_counts.get('low', 0)} low)."
                )
            sections.append(("New security alerts", "\n".join(lines)))
        elif open_counts.get("open_total"):
            sections.append((
                "New security alerts",
                f"No new alerts. {open_counts['open_total']} remain open "
                f"({open_counts.get('high', 0)} high).",
            ))

    research = digest.get("research") or {}
    negatives = research.get("negatives") or []
    if negatives:
        lines = ["Sources speaking negatively about the brand:"]
        lines.extend(
            f"- {n['domain']} (sentiment {n['sentiment']}): {n['url']}"
            for n in negatives
        )
        sections.append(("Watchouts", "\n".join(lines)))

    outreach_lines = []
    for thread in research.get("threads") or []:
        competitors = ", ".join(thread.get("competitors") or []) or "competitors"
        outreach_lines.append(
            f"- Thread on {thread['domain']} mentions {competitors} but not "
            f"you: {thread['url']}"
        )
    for question in research.get("questions") or []:
        line = f"- People also ask: {question['question']}"
        if question.get("url"):
            line += f" ({question['url']})"
        outreach_lines.append(line)
    for domain in digest.get("influence_domains") or []:
        outreach_lines.append(
            f"- {domain['domain']} keeps getting cited for competitors; "
            "worth a placement."
        )
    if outreach_lines:
        sections.append(("Outreach targets", "\n".join(outreach_lines)))

    if research:
        answered = research.get("engines_answered", 0)
        if answered:
            sections.append((
                "Engine coverage",
                f"{research.get('engines_naming_brand', 0)} of {answered} AI "
                f"engines named the brand for \"{research.get('query', '')}\".",
            ))

    actions = digest.get("actions") or []
    if actions:
        sections.append(("Action lines", "\n".join(f"- {a}" for a in actions)))

    return sections


def _headline(digest: dict) -> str:
    stamp = timezone.now().strftime("%b %d")
    return f"Brand Pulse - {digest.get('website_name', '')} - {stamp}"


def render_text(digest: dict) -> str:
    """Plain-text digest body (Teams, in-app fallback)."""
    parts = [
        f"{heading}\n{body}" for heading, body in _section_lines(digest)
    ]
    if not parts:
        parts = ["No notable changes this period."]
    return "\n\n".join(parts)


def render_sms(digest: dict) -> str:
    """Two-SMS-segment summary (300 chars) for phones.

    A text cannot carry the report, so it carries the triage: what is new,
    how bad, and which reply command fetches the detail. Lands in the
    iPhone Messages app via the existing Twilio lane.
    """
    name = digest.get("website_name") or "your brand"
    fragments = []
    security = digest.get("security") or {}
    new_count = security.get("new_count") or 0
    if new_count:
        fragment = f"{new_count} new security alert{'s' if new_count != 1 else ''}"
        if any(
            a.get("severity") == "high" for a in security.get("new_top") or []
        ):
            fragment += " incl HIGH"
        fragments.append(fragment)
    research = digest.get("research") or {}
    negatives = research.get("negatives") or []
    if negatives:
        fragments.append(
            f"negative sentiment on {negatives[0].get('domain', 'a source')}"
        )
    threads = len(research.get("threads") or [])
    if threads:
        fragments.append(
            f"{threads} thread{'s' if threads != 1 else ''} to join"
        )
    visibility = digest.get("visibility") or {}
    delta = visibility.get("delta_pct")
    if delta is not None and abs(delta) >= 5:
        direction = "up" if delta > 0 else "down"
        fragments.append(f"visibility {direction} {abs(round(delta))}%")

    summary = "; ".join(fragments) if fragments else "no notable changes"
    body = (
        f"Cansee Brand Pulse ({name}): {summary}. "
        "Reply 'report' or 'security' for details."
    )
    return _clip(body, 300)


def render_slack_blocks(digest: dict) -> list[dict]:
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": _clip(_headline(digest), 150)},
        },
        {"type": "divider"},
    ]
    for heading, body in _section_lines(digest):
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": _clip(f"*{heading}*\n{body}", SLACK_TEXT_LIMIT),
            },
        })
    if len(blocks) == 2:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "No notable changes this period."},
        })
    return blocks


def render_discord(digest: dict) -> dict:
    """Kwargs for DiscordService.send_message (title/description/fields)."""
    fields = [
        {
            "name": _clip(heading, 256),
            "value": _clip(body, DISCORD_FIELD_LIMIT),
            "inline": False,
        }
        for heading, body in _section_lines(digest)
    ]
    return {
        "title": _clip(_headline(digest), 250),
        "description": _clip(
            "Your periodic brand digest from Cansee.", DISCORD_DESCRIPTION_LIMIT,
        ),
        "fields": fields,
        "footer": "Cansee Brand Pulse",
    }


# ── Delivery ─────────────────────────────────────────────────────────────────


def deliver_digest(website) -> bool:
    """Build and push the digest. Returns True when anything was delivered."""
    pulse = _get_pulse(website)
    digest = build_digest(website)
    if digest is None:
        return False

    if pulse.auto_scan:
        try:
            if maybe_queue_research_scan(website):
                digest["actions"].append(
                    "Queued a fresh Brand Research scan; results will appear "
                    "on the Brand Research page shortly."
                )
        except Exception as exc:
            logger.warning(
                "brand pulse auto-scan failed for website %s: %s", website.id, exc,
            )

    from apps.notifications.models import IntegrationConnection
    from apps.notifications.services.discord_service import DiscordService
    from apps.notifications.services.slack_service import SlackService
    from apps.notifications.services.teams_service import TeamsService

    # webhook_url is an EncryptedTextField: iterate instances so decryption
    # happens per-row (never .values_list on it).
    connections = list(
        IntegrationConnection.objects.filter(user=website.user, is_active=True)
    )
    delivered = False
    for connection in connections:
        try:
            if connection.platform == "slack":
                ok = SlackService.send_message(
                    webhook_url=connection.webhook_url,
                    text=_clip(_headline(digest), SLACK_TEXT_LIMIT),
                    blocks=render_slack_blocks(digest),
                )
            elif connection.platform == "discord":
                ok = DiscordService.send_message(
                    webhook_url=connection.webhook_url, **render_discord(digest),
                )
            elif connection.platform == "teams":
                ok = TeamsService.send_message(
                    webhook_url=connection.webhook_url,
                    title=_headline(digest),
                    text=render_text(digest),
                )
            else:
                continue
            delivered = delivered or bool(ok)
        except Exception as exc:
            logger.warning(
                "brand pulse delivery failed for %s (%s): %s",
                website.id, connection.platform, exc,
            )

    # SMS lane: lands in the iPhone Messages app via Twilio. Recurring
    # texts are the heaviest consent ask, so this requires the explicit
    # pulse_digest opt-in on a verified number — never inferred from the
    # number's alert consent.
    sms_sent = False
    try:
        from apps.notifications.models import SmsSubscription
        from apps.notifications.services import sms_service

        for subscription in SmsSubscription.objects.filter(
            user=website.user, pulse_digest=True,
        ):
            if subscription.is_active and sms_service.notify(
                subscription, render_sms(digest)
            ):
                sms_sent = True
        delivered = delivered or sms_sent
    except Exception as exc:
        logger.warning(
            "brand pulse sms delivery failed for %s: %s", website.id, exc,
        )

    if not connections and not sms_sent:
        try:
            from apps.notifications.services.notification_service import (
                NotificationService,
            )

            NotificationService.create(
                user=website.user,
                notification_type="brand_pulse_digest",
                title=_headline(digest),
                message=_clip(render_text(digest), 2000),
                data={"website_id": str(website.id)},
                action_url=DIGEST_ACTION_URL.format(website_id=website.id),
            )
            delivered = True
        except Exception as exc:
            logger.warning(
                "brand pulse in-app fallback failed for %s: %s", website.id, exc,
            )

    if delivered:
        pulse.last_digest_at = timezone.now()
        pulse.save(update_fields=["last_digest_at", "updated_at"])
    return delivered


def maybe_queue_research_scan(website) -> bool:
    """Queue a Brand Research scan when the data is stale and spend allows.

    Scan analysis bills unmetered inside the pipeline, so the allowance
    pre-check here is mandatory, not a courtesy.
    """
    from django.conf import settings

    from apps.citations.models import SourceScan, SourceScanStatus
    from core.llm.base import allowance_denial

    pulse = _get_pulse(website)
    now = timezone.now()

    if not getattr(settings, "PERPLEXITY_API_KEY", ""):
        return False
    query = (website.name or "").strip()
    if not query:
        return False
    if SourceScan.objects.filter(
        website=website,
        status=SourceScanStatus.COMPLETE,
        completed_at__gte=now - timedelta(days=FRESH_SCAN_DAYS),
    ).exists():
        return False
    if (
        pulse.last_scan_queued_at
        and pulse.last_scan_queued_at > now - timedelta(days=SCAN_QUEUE_COOLDOWN_DAYS)
    ):
        return False

    from apps.citations.api.v1.views import SourceScanListCreateView

    active = SourceScan.objects.filter(
        website=website,
        status__in=[SourceScanStatus.PENDING, SourceScanStatus.RUNNING],
    ).count()
    if active >= SourceScanListCreateView.MAX_ACTIVE_PER_WEBSITE:
        return False
    if allowance_denial(website.user):
        return False

    from apps.citations.tasks import run_source_scan

    scan = SourceScan.objects.create(
        website=website, query=query, created_by=website.user,
    )
    run_source_scan.delay(str(scan.id))
    pulse.last_scan_queued_at = now
    pulse.save(update_fields=["last_scan_queued_at", "updated_at"])
    logger.info("brand pulse queued research scan %s for %s", scan.id, website.id)
    return True


def push_new_high_alerts(website, alerts) -> None:
    """Immediately push a scan's new high-severity alerts to chat platforms.

    One message per batch, not per alert. Fully guarded: called from inside
    the response auditor, so a push failure must never break a scan.
    """
    try:
        from apps.brand_vault.models import BrandPulse
        from apps.notifications.models import IntegrationConnection
        from apps.notifications.services.discord_service import DiscordService
        from apps.notifications.services.slack_service import SlackService
        from apps.notifications.services.teams_service import TeamsService

        high = [
            a for a in (alerts or [])
            if a is not None and getattr(a, "severity", "") == "high"
        ]
        if not high:
            return
        pulse = BrandPulse.objects.filter(website=website).first()
        if pulse is None or not pulse.enabled:
            return
        from apps.notifications.models import SmsSubscription

        connections = list(
            IntegrationConnection.objects.filter(
                user=website.user, is_active=True,
            )
        )
        # High-severity security texts are exactly what the SMS channel's
        # alert_security consent covers — no extra opt-in needed here.
        sms_subscriptions = [
            s for s in SmsSubscription.objects.filter(
                user=website.user, alert_security=True,
            )
            if s.is_active
        ]
        if not connections and not sms_subscriptions:
            return

        name = website.name or website.url or "your brand"
        count = len(high)
        plural = "s" if count != 1 else ""
        lines = [
            f"{count} new high-severity brand security finding{plural} for {name}:"
        ]
        for alert in high:
            line = (
                f"- {alert.reference} [{alert.issue}] "
                f"{alert.title or alert.get_issue_display()}"
            )
            if alert.source_url:
                line += f" ({alert.source_url})"
            lines.append(_clip(line, 500))
        body = "\n".join(lines)

        delivered = False
        for connection in connections:
            try:
                if connection.platform == "slack":
                    ok = SlackService.send_message(
                        webhook_url=connection.webhook_url,
                        text=_clip(f"Brand security alert\n{body}", SLACK_TEXT_LIMIT),
                    )
                elif connection.platform == "discord":
                    ok = DiscordService.send_message(
                        webhook_url=connection.webhook_url,
                        title="Brand security alert",
                        description=_clip(body, DISCORD_DESCRIPTION_LIMIT),
                        footer="Cansee Brand Pulse",
                    )
                elif connection.platform == "teams":
                    ok = TeamsService.send_message(
                        webhook_url=connection.webhook_url,
                        title="Brand security alert",
                        text=body,
                    )
                else:
                    continue
                delivered = delivered or bool(ok)
            except Exception as exc:
                logger.warning(
                    "brand pulse alert push failed for %s (%s): %s",
                    website.id, connection.platform, exc,
                )

        if sms_subscriptions:
            from apps.notifications.services import sms_service

            sms_body = _clip(
                f"Cansee alert: {count} new high-severity brand finding"
                f"{plural} for {name}. Reply 'security' for details.",
                300,
            )
            for subscription in sms_subscriptions:
                try:
                    if sms_service.notify(subscription, sms_body):
                        delivered = True
                except Exception as exc:
                    logger.warning(
                        "brand pulse alert sms failed for %s: %s", website.id, exc,
                    )

        if delivered:
            pulse.last_alert_push_at = timezone.now()
            pulse.save(update_fields=["last_alert_push_at", "updated_at"])
    except Exception as exc:
        logger.warning(
            "brand pulse alert push failed for website %s: %s",
            getattr(website, "id", "?"), exc,
        )
