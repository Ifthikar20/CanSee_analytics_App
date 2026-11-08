"""The usage dashboard (design/p2_contract.md section 14, "Command Center"
in the design only): agent runs, drafts and AI spend of one person or of
an organization, from AgentRun, AgentSwarmVersion, AgentArtifactVersion
and the usage ledger (AITokenUsage). Every number is a count or a sum of
recorded rows; nothing is estimated.

A run is an AgentRun created in the window (every trigger, re-runs
included). A writing agent counts a run when the run has a ledger row of
its own (role ``subagent:{key}``), so a failed draft counts too; the
research agents count runs with an AgentSwarmVersion; ``agent_only`` is a
run with neither.
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, date, timedelta
from decimal import Decimal

from django.db.models import Count, Max, Sum
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import TruncDate
from django.utils import timezone

from apps.agent.constants import (
    ACTIVE_RUN_STATUSES,
    AGENT_TEAMS,
    RESEARCH_AGENTS_KEY,
    TEAM_LABELS,
    TEAMS,
    USAGE_MODULE,
    RunStatus,
    VersionSource,
)
from apps.agent.models import AgentArtifactVersion, AgentRun, AgentSwarmVersion
from core.exceptions import CanseeException, PermissionDenied, ResourceNotFound

CHUNK = 1000
TOP_USERS = 20
AGENT_ONLY_KEY = "agent_only"
FINAL_STATUSES = (RunStatus.COMPLETED.value, RunStatus.FAILED.value,
                  RunStatus.CANCELLED.value, RunStatus.INTERRUPTED.value)
# Spend categories in display order (contract 14b).
CATEGORIES = (
    ("agent_replies", "Agent replies"),
    ("writers", "Writing agents"),
    ("research", "Research agents"),
    ("context", "Context builds"),
    ("engines", "AI engine answers"),
    ("insights", "Insights runs"),
    ("other", "Other"),
)
ORG_ONLY_MESSAGE = "Only organization owners and admins can see the whole organization."
_CENT = Decimal("0.0001")


def _usd(value) -> str:
    return str(Decimal(value or 0).quantize(_CENT))


def _invalid(field: str, message: str) -> CanseeException:
    return CanseeException(message, code="invalid", status_code=400,
                           details={"field": field, "message": message})


def category(module: str, role: str | None) -> str:
    role = role or ""
    if module == USAGE_MODULE:
        if role in ("orchestrator", "title"):
            return "agent_replies"
        if role.startswith("subagent:"):
            return "writers"
        if role == "worker":
            return "research"
        if role == "context_build":
            return "context"
        return "other"
    if module == "llm_ranking":
        return "engines"
    if module == "insights":
        return "insights"
    return "other"


def _membership(user):
    """The user's first organization membership (core.permissions.org)."""
    return user.org_memberships.select_related("organization").order_by("created_at").first()


def scopes_for(user) -> tuple[list[dict], object | None]:
    """The scopes the user may read, and the organization of ``org``."""
    from core.permissions.org import org_features_enabled
    from core.permissions.rbac import role_at_least

    scopes = [{"key": "me", "label": "Your activity"}]
    org = None
    if org_features_enabled():
        membership = _membership(user)
        if membership is not None and role_at_least(membership.role, "admin"):
            org = membership.organization
            scopes.append({"key": "org", "label": org.name})
    return scopes, org


def _websites(user, scope: str, org):
    from apps.websites.models import Website
    from apps.websites.services.website_service import WebsiteService

    qs = Website.objects.filter(organization=org) if scope == "org" else (
        WebsiteService.accessible_qs(user))
    return list(qs.order_by("name", "id").values("id", "name"))


def _bucket_index(window, day: date) -> int | None:
    if window.interval == "week":
        day = day - timedelta(days=day.weekday())
    return window._index.get(day)


def _chunks(ids: list) -> list[list]:
    return [ids[i:i + CHUNK] for i in range(0, len(ids), CHUNK)]


def build(user, *, scope=None, period=None, interval=None, website_id=None) -> dict:
    """GET command-center/ (contract 14b)."""
    from apps.accounts.models import AITokenUsage
    from apps.llm_ranking.services.answer_window import Window, parse_filters

    period, interval, _, _ = parse_filters(period=period, interval=interval)
    scopes, org = scopes_for(user)
    allowed = [s["key"] for s in scopes]
    if scope in (None, ""):
        scope = "org" if "org" in allowed else "me"
    elif scope not in ("me", "org"):
        raise _invalid("scope", "Choose a scope of me or org.")
    elif scope not in allowed:
        raise PermissionDenied(ORG_ONLY_MESSAGE)
    websites = _websites(user, scope, org)
    site = None
    if website_id:
        try:
            site = uuid.UUID(str(website_id))
        except ValueError:
            raise _invalid("website_id", "This is not a project id.") from None
        if site not in {w["id"] for w in websites}:
            raise ResourceNotFound("Project not found.")

    window = Window(period, interval, timezone.now().astimezone(UTC).date())
    start, prev_start = window.start_at, window.prev_start_at

    runs = AgentRun.objects.all()
    ledger = AITokenUsage.objects.all()
    if scope == "org":
        runs = runs.filter(website__organization=org)
        ledger = ledger.filter(website__organization=org)
    else:
        runs = runs.filter(user=user)
        ledger = ledger.filter(user=user)
    if site is not None:
        runs = runs.filter(website_id=site)
        ledger = ledger.filter(website_id=site)

    in_window = runs.filter(created_at__gte=start)
    rows = list(in_window.annotate(day=TruncDate("created_at", tzinfo=UTC))
                .values("id", "day", "status", "user_id", "chat_id", "created_at"))
    prev_rows = list(runs.filter(created_at__gte=prev_start, created_at__lt=start)
                     .values("user_id"))
    n = len(window.buckets)

    totals = {
        "runs": len(rows), "runs_prev": len(prev_rows),
        **dict.fromkeys(("completed", "failed", "cancelled", "interrupted"), 0),
        "in_progress": 0,
        "chats": len({r["chat_id"] for r in rows}),
        "drafts_saved": 0,
        "active_users": len({r["user_id"] for r in rows}),
        "active_users_prev": len({r["user_id"] for r in prev_rows}),
    }
    runs_series = {status: [0] * n for status in FINAL_STATUSES}
    users_by_bucket: list[set] = [set() for _ in range(n)]
    for r in rows:
        if r["status"] in ACTIVE_RUN_STATUSES:
            totals["in_progress"] += 1
        elif r["status"] in totals:
            totals[r["status"]] += 1
        i = _bucket_index(window, r["day"])
        if i is None:
            continue
        users_by_bucket[i].add(r["user_id"])
        if r["status"] in runs_series:
            runs_series[r["status"]][i] += 1

    # Spend: the ledger rows of the window, by bucket, module, role and run.
    usage = list(
        ledger.filter(created_at__gte=start)
        .annotate(day=TruncDate("created_at", tzinfo=UTC),
                  role=KeyTextTransform("role", "metadata"),
                  run=KeyTextTransform("run_id", "metadata"))
        .values("day", "module", "role", "run")
        .annotate(usd=Sum("estimated_cost_usd"), requests=Count("id"))
    )
    keys = [key for key, _ in CATEGORIES]
    spend_series = {key: [Decimal("0")] * n for key in keys}
    spend = {key: {"usd": Decimal("0"), "requests": 0} for key in keys}
    writer_runs: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    worker_usd: dict[str, Decimal] = defaultdict(Decimal)
    for u in usage:
        key = category(u["module"], u["role"])
        usd = u["usd"] or Decimal("0")
        spend[key]["usd"] += usd
        spend[key]["requests"] += u["requests"]
        i = _bucket_index(window, u["day"])
        if i is not None:
            spend_series[key][i] += usd
        role = u["role"] or ""
        if u["module"] == USAGE_MODULE and u["run"]:
            if role.startswith("subagent:"):
                writer_runs[u["run"]][role.split(":", 1)[1]] += usd
            elif role == "worker":
                worker_usd[u["run"]] += usd
    ai_usd = sum((s["usd"] for s in spend.values()), Decimal("0"))
    ai_usd_prev = (ledger.filter(created_at__gte=prev_start, created_at__lt=start)
                   .aggregate(total=Sum("estimated_cost_usd"))["total"]) or Decimal("0")

    run_ids = [r["id"] for r in rows]
    swarm_runs: set = set()
    drafts: dict = defaultdict(lambda: defaultdict(int))
    for chunk in _chunks(run_ids):
        swarm_runs.update(AgentSwarmVersion.objects.filter(run_id__in=chunk)
                          .values_list("run_id", flat=True).distinct())
        for d in (AgentArtifactVersion.objects
                  .filter(run_id__in=chunk, source=VersionSource.AGENT)
                  .values("run_id", "artifact__agent_key").annotate(n=Count("id"))):
            drafts[d["run_id"]][d["artifact__agent_key"]] += d["n"]
    totals["drafts_saved"] = sum(sum(per.values()) for per in drafts.values())

    by_agent, by_team = _by_agent_and_team(run_ids, writer_runs, worker_usd, swarm_runs, drafts)
    totals["ai_usd"] = _usd(ai_usd)
    totals["ai_usd_prev"] = _usd(ai_usd_prev)
    no_activity = not rows and not usage
    return {
        "scope": scope,
        "scopes": scopes,
        "window": {
            "period": period,
            "interval": interval,
            "start": window.start.isoformat(),
            "end": window.end.isoformat(),
            "buckets": [day.isoformat() for day in window.buckets],
        },
        "filters": {"website_id": str(site) if site else None},
        "websites": [{"id": str(w["id"]), "name": w["name"]} for w in websites],
        "empty_reason": "no_activity" if no_activity else None,
        "totals": totals,
        "runs_series": runs_series,
        "active_users_series": [len(users) for users in users_by_bucket],
        "spend_series": {key: [_usd(v) for v in values] for key, values in spend_series.items()},
        "spend_by_category": [
            {"key": key, "label": label, "usd": _usd(spend[key]["usd"]),
             "requests": spend[key]["requests"]}
            for key, label in CATEGORIES
        ],
        "by_agent": by_agent,
        "by_team": by_team,
        "top_users": _top_users(org, rows, usage_rows=ledger.filter(created_at__gte=start))
        if scope == "org" else None,
    }


def _by_agent_and_team(run_ids, writer_runs, worker_usd, swarm_runs, drafts):
    from apps.agent.services.catalog import agents as catalog_agents

    writers = [a for a in catalog_agents() if a["key"] in _writer_keys()]
    stats = {a["key"]: {"runs": 0, "drafts_saved": 0, "ai_usd": Decimal("0")} for a in writers}
    stats[RESEARCH_AGENTS_KEY] = {"runs": 0, "drafts_saved": 0, "ai_usd": Decimal("0")}
    stats[AGENT_ONLY_KEY] = {"runs": 0, "drafts_saved": 0, "ai_usd": Decimal("0")}
    teams = {key: 0 for key, _ in TEAMS}
    for run_id in run_ids:
        rid = str(run_id)
        used = [key for key in writer_runs.get(rid, {}) if key in stats]
        swarm = run_id in swarm_runs
        for key in used:
            stats[key]["runs"] += 1
            stats[key]["ai_usd"] += writer_runs[rid][key]
        if swarm:
            stats[RESEARCH_AGENTS_KEY]["runs"] += 1
            stats[RESEARCH_AGENTS_KEY]["ai_usd"] += worker_usd.get(rid, Decimal("0"))
        if not used and not swarm:
            stats[AGENT_ONLY_KEY]["runs"] += 1
        for agent_key, count in drafts.get(run_id, {}).items():
            if agent_key in stats and agent_key not in (RESEARCH_AGENTS_KEY, AGENT_ONLY_KEY):
                stats[agent_key]["drafts_saved"] += count
            else:
                # A saved report belongs to the reply that wrote it.
                owner = RESEARCH_AGENTS_KEY if swarm else AGENT_ONLY_KEY
                stats[owner]["drafts_saved"] += count
        primary = {AGENT_TEAMS[key][0] for key in used}
        if swarm:
            primary.add(AGENT_TEAMS[RESEARCH_AGENTS_KEY][0])
        for team in primary:
            teams[team] += 1
    rows = [(a["key"], a["name"]) for a in writers]
    rows += [(RESEARCH_AGENTS_KEY, "Research agents"), (AGENT_ONLY_KEY, "Agent only")]
    by_agent = [
        {"key": key, "label": label,
         "team": AGENT_TEAMS[key][0] if key in AGENT_TEAMS else None,
         "runs": stats[key]["runs"], "drafts_saved": stats[key]["drafts_saved"],
         "ai_usd": _usd(stats[key]["ai_usd"])}
        for key, label in rows if stats[key]["runs"] or stats[key]["drafts_saved"]
    ]
    by_team = [{"key": key, "label": TEAM_LABELS[key], "runs": teams[key]} for key, _ in TEAMS]
    return by_agent, by_team


def _writer_keys() -> set[str]:
    from apps.agent.constants import SUBAGENT_KEYS

    return set(SUBAGENT_KEYS)


def _top_users(org, rows, *, usage_rows) -> list[dict]:
    """The organization's 20 most active people in the window."""
    from apps.accounts.models import OrganizationMember, User
    from apps.agent.api.v1.serializers import display_name, dt

    per: dict = defaultdict(lambda: {"runs": 0, "days": set(), "usd": Decimal("0"),
                                     "last": None})
    for r in rows:
        person = per[r["user_id"]]
        person["runs"] += 1
        person["days"].add(r["day"])
        if person["last"] is None or r["created_at"] > person["last"]:
            person["last"] = r["created_at"]
    for row in (usage_rows.exclude(user__isnull=True).values("user_id")
                .annotate(usd=Sum("estimated_cost_usd"), last=Max("created_at"))):
        person = per[row["user_id"]]
        person["usd"] += row["usd"] or Decimal("0")
        if person["last"] is None or (row["last"] and row["last"] > person["last"]):
            person["last"] = row["last"]
    ranked = sorted(per.items(), key=lambda item: (-item[1]["runs"], -item[1]["usd"],
                                                   str(item[0])))[:TOP_USERS]
    users = User.objects.in_bulk([user_id for user_id, _ in ranked])
    roles = dict(OrganizationMember.objects.filter(organization=org,
                                                   user_id__in=[u for u, _ in ranked])
                 .values_list("user_id", "role"))
    out = []
    for user_id, person in ranked:
        user = users.get(user_id)
        if user is None:
            continue
        role = roles.get(user_id)
        name = display_name(user)
        out.append({
            "user_id": str(user_id),
            "name": name if role else f"{name} (former member)",
            "email": user.email,
            "role": role,
            "runs": person["runs"],
            "active_days": len(person["days"]),
            "ai_usd": _usd(person["usd"]),
            "last_active_at": dt(person["last"]),
        })
    return out
