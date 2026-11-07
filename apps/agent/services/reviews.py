"""Approvals (design/p2_contract.md section 13): review states on every
draft, a reviewer from the project's team, comments, inbox items and email.

Nothing here publishes anything: an approval records who approved which
version, and the draft is exported as before. A review row is created on
the first action; an artifact without one is a draft. A newer version
never inherits an approval (``effective_state``). Every action appends an
event and moves ``revision`` on, so two people acting at once cannot both
win; comments leave the state and the revision alone.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.agent.constants import ReviewEmail, ReviewEventKind, ReviewState
from apps.agent.models import AgentArtifact, AgentArtifactReview, AgentReviewEvent
from apps.agent.services.access import can_write, is_project_admin
from apps.agent.services.team import team_for
from apps.agent.tools.shaping import clip
from core.exceptions import CanseeException, PermissionDenied

logger = logging.getLogger("apps")

NOTE_MAX_LENGTH = 4000
EXCERPT_CHARS = 140
LIST_LIMIT = 100
APPROVED_DAYS = 90
EVENTS_LIMIT = 200
BOXES = ("waiting", "mine", "approved")
ACTIONS = ("request", "approve", "request_changes", "withdraw", "reopen")

DRAFT = ReviewState.DRAFT.value
IN_REVIEW = ReviewState.IN_REVIEW.value
CHANGES = ReviewState.CHANGES_REQUESTED.value
APPROVED = ReviewState.APPROVED.value

# 409 review_state, by the state the review is in now (contract 13c).
STATE_MESSAGES = {
    DRAFT: "This draft isn't under review now. Reload to see the latest.",
    IN_REVIEW: "This draft is in review now. Reload to see the latest.",
    CHANGES: "Changes were requested on this draft. Reload to see the latest.",
    APPROVED: "This draft is approved now. Reload to see the latest.",
}
# The effective states each action starts from (contract 13b).
FROM_STATES = {
    "request": (DRAFT, CHANGES),
    "approve": (IN_REVIEW, DRAFT, CHANGES),
    "request_changes": (IN_REVIEW,),
    "withdraw": (IN_REVIEW,),
    "reopen": (APPROVED, CHANGES),
}
EVENT_KINDS = {
    "request": ReviewEventKind.REQUESTED.value,
    "approve": ReviewEventKind.APPROVED.value,
    "request_changes": ReviewEventKind.CHANGES_REQUESTED.value,
    "withdraw": ReviewEventKind.WITHDRAWN.value,
    "reopen": ReviewEventKind.REOPENED.value,
}
# Inbox item types (contract 13d), by event kind.
INBOX_TYPES = {
    ReviewEventKind.REQUESTED.value: "review_requested",
    ReviewEventKind.APPROVED.value: "review_approved",
    ReviewEventKind.CHANGES_REQUESTED.value: "review_changes_requested",
    ReviewEventKind.COMMENT.value: "review_comment",
}


class ReviewConflict(CanseeException):
    def __init__(self, revision: int):
        super().__init__("Someone else changed this review. Reload to see it.",
                         code="revision_conflict", status_code=409,
                         details={"revision": revision})


class ReviewStateError(CanseeException):
    def __init__(self, state: str):
        super().__init__(STATE_MESSAGES.get(state, STATE_MESSAGES[DRAFT]),
                         code="review_state", status_code=409)


class NoReviewers(CanseeException):
    def __init__(self):
        super().__init__("Add a teammate to this project to ask for a review.",
                         code="no_reviewers", status_code=400)


class NoVersion(CanseeException):
    def __init__(self):
        super().__init__("This draft has no saved version yet.", code="no_version",
                         status_code=400)


class ReviewInvalid(CanseeException):
    def __init__(self, field: str, message: str):
        super().__init__(message, code="invalid", status_code=400,
                         details={"fields": {field: [message]}})


def _name(user) -> str | None:
    from apps.agent.api.v1.serializers import display_name

    return display_name(user) if user is not None else None


def _person(user) -> dict | None:
    return {"id": str(user.id), "name": _name(user)} if user is not None else None


def _review_of(artifact) -> AgentArtifactReview | None:
    try:
        return artifact.review
    except ObjectDoesNotExist:
        return None


def effective_state(review, latest_version: int) -> str:
    """``draft`` once a newer version than the approved one exists."""
    if review is None:
        return DRAFT
    if review.state == APPROVED and review.version < latest_version:
        return DRAFT
    return review.state


def summary_for(artifact) -> dict:
    """The ``review`` block of ArtifactSummary and ArtifactDetail."""
    from apps.agent.api.v1.serializers import dt

    review = _review_of(artifact)
    if review is None:
        return {"state": DRAFT, "effective_state": DRAFT, "version": None,
                "reviewer_name": None, "updated_at": None}
    reviewer = review.reviewer if review.reviewer_id else None
    return {
        "state": review.state,
        "effective_state": effective_state(review, artifact.latest_version),
        "version": review.version or None,
        "reviewer_name": _name(reviewer),
        "updated_at": dt(review.updated_at),
    }


# ── Who may do what ───────────────────────────────────────────────────────


def _is_decider(user, website, review, team_ids: set) -> bool:
    """The reviewer while still on the team, the website's owner, or an
    admin or owner of the website's own organization."""
    if review is not None and review.reviewer_id == user.id and user.id in team_ids:
        return True
    return is_project_admin(user, website)


def _abilities(user, website, review, latest_version: int, team: list[dict]) -> dict:
    if not can_write(user, website):
        return dict.fromkeys(("request", "approve", "request_changes", "withdraw", "reopen",
                              "comment"), False)
    ids = {m["id"] for m in team}
    state = effective_state(review, latest_version)
    decider = _is_decider(user, website, review, ids)
    sole = len(team) == 1 and team[0]["id"] == user.id
    requester = review is not None and review.requested_by_id == user.id
    has_version = latest_version > 0
    return {
        "request": has_version and state in (DRAFT, CHANGES),
        "approve": has_version and ((state == IN_REVIEW and decider)
                                    or (state in (DRAFT, CHANGES) and sole)),
        "request_changes": state == IN_REVIEW and decider,
        "withdraw": state == IN_REVIEW and (requester or decider),
        "reopen": state in (APPROVED, CHANGES),
        "comment": True,
    }


# ── Reads ─────────────────────────────────────────────────────────────────


def event_payload(event) -> dict:
    from apps.agent.api.v1.serializers import dt

    return {
        "id": str(event.id),
        "kind": event.kind,
        "actor_name": _name(event.actor) if event.actor_id else None,
        "reviewer_name": _name(event.reviewer) if event.reviewer_id else None,
        "version": event.version,
        "body": event.body,
        "email": event.email,
        "created_at": dt(event.created_at),
    }


def review_payload(artifact, user) -> dict:
    """Contract ``Review``."""
    from apps.agent.api.v1.serializers import dt

    review = (AgentArtifactReview.objects
              .select_related("reviewer", "requested_by", "decided_by")
              .filter(artifact_id=artifact.id).first())
    latest = (AgentArtifact.objects.filter(pk=artifact.pk)
              .values_list("latest_version", flat=True).first()) or 0
    team = team_for(artifact.website)
    events = list(AgentReviewEvent.objects.filter(artifact_id=artifact.id)
                  .select_related("actor", "reviewer").order_by("-created_at")[:EVENTS_LIMIT])
    events.reverse()
    state = review.state if review else DRAFT
    return {
        "artifact_id": str(artifact.id),
        "state": state,
        "effective_state": effective_state(review, latest),
        "version": review.version if review and review.version else None,
        "latest_version": latest,
        "updated_since_request": bool(review and state == IN_REVIEW and latest > review.version),
        "reviewer": _person(review.reviewer) if review and review.reviewer_id else None,
        "requested_by": (_person(review.requested_by)
                         if review and review.requested_by_id else None),
        "requested_at": dt(review.requested_at) if review else None,
        "decided_by": _person(review.decided_by) if review and review.decided_by_id else None,
        "decided_at": dt(review.decided_at) if review else None,
        "revision": review.revision if review else 0,
        "sole_writer": len(team) == 1 and team[0]["id"] == user.id,
        "can": _abilities(user, artifact.website, review, latest, team),
        "events": [event_payload(e) for e in events],
    }


def team_payload(website, user) -> list[dict]:
    """``team/``: the members who can write, with their emails."""
    return [{"id": str(m["id"]), "name": m["name"], "email": m["email"], "role": m["role"],
             "is_you": m["id"] == user.id} for m in team_for(website)]


def list_reviews(user, website, box: str) -> list:
    """Artifacts for one box of the Reviews page, newest first."""
    reviews = (AgentArtifactReview.objects
               .filter(website=website, artifact__deleted_at__isnull=True)
               .select_related("artifact", "artifact__created_by", "artifact__source_chat",
                               "reviewer"))
    if box == "waiting":
        reviews = reviews.filter(reviewer=user, state=IN_REVIEW)
    elif box == "mine":
        reviews = reviews.filter(requested_by=user, state__in=(IN_REVIEW, CHANGES))
    else:
        since = timezone.now() - timedelta(days=APPROVED_DAYS)
        reviews = reviews.filter(state=APPROVED, decided_at__gte=since)
    out = []
    for review in reviews.order_by("-updated_at")[:LIST_LIMIT * 2]:
        artifact = review.artifact
        if box == "approved" and effective_state(review, artifact.latest_version) != APPROVED:
            continue
        # The artifact's own cache holds the review already read.
        artifact.review = review
        out.append(artifact)
        if len(out) >= LIST_LIMIT:
            break
    return out


def inbox_items(user, website, limit: int = 20) -> list[dict]:
    """Review items of the activity feed (contract 13d), newest first;
    nobody sees their own actions."""
    events = (
        AgentReviewEvent.objects
        .filter(website=website, artifact__deleted_at__isnull=True)
        .exclude(actor=user)
        .filter(
            Q(recipient=user, kind__in=(ReviewEventKind.REQUESTED, ReviewEventKind.APPROVED,
                                        ReviewEventKind.CHANGES_REQUESTED))
            | (Q(kind=ReviewEventKind.COMMENT)
               & (Q(artifact__review__reviewer=user) | Q(artifact__review__requested_by=user)))
        )
        .select_related("actor", "artifact")
        .order_by("-created_at")[:limit]
    )
    items = []
    for event in events:
        item = {
            "type": INBOX_TYPES[event.kind],
            "at": event.created_at,
            "artifact_id": event.artifact_id,
            "title": event.artifact.title,
            "kind": event.artifact.kind,
            "version": event.version,
            "actor_name": _name(event.actor) if event.actor_id else None,
        }
        if event.kind != ReviewEventKind.REQUESTED:
            item["excerpt"] = clip(event.body, EXCERPT_CHARS)
        items.append(item)
    return items


# ── Actions ───────────────────────────────────────────────────────────────


def _clean_note(note) -> str:
    return str(note or "").replace("\x00", "").strip()


def act(artifact, user, *, action: str, revision: int, reviewer_id=None,
        note: str = "") -> AgentArtifactReview:
    """One review action (contract 13b), checked against the review as it
    is now, under a row lock."""
    website = artifact.website
    note = _clean_note(note)
    if len(note) > NOTE_MAX_LENGTH:
        raise ReviewInvalid("note", f"Notes are at most {NOTE_MAX_LENGTH:,} characters.")
    with transaction.atomic():
        AgentArtifactReview.objects.get_or_create(artifact_id=artifact.id,
                                                  defaults={"website_id": website.id})
        review = AgentArtifactReview.objects.select_for_update().get(artifact_id=artifact.id)
        if revision != review.revision:
            raise ReviewConflict(review.revision)
        latest = (AgentArtifact.objects.filter(pk=artifact.pk)
                  .values_list("latest_version", flat=True).first()) or 0
        if latest == 0:
            raise NoVersion()
        state = effective_state(review, latest)
        if state not in FROM_STATES[action]:
            raise ReviewStateError(state)
        team = team_for(website)
        ids = {m["id"] for m in team}
        decider = _is_decider(user, website, review, ids)
        now = timezone.now()
        event = {"kind": EVENT_KINDS[action], "version": latest, "body": note}

        if action == "request":
            teammates = [m for m in team if m["id"] != user.id]
            if not teammates:
                raise NoReviewers()
            reviewer = next((m["user"] for m in teammates if str(m["id"]) == str(reviewer_id)),
                            None)
            if reviewer is None:
                raise ReviewInvalid("reviewer_id", "Choose a teammate who can edit this project.")
            review.state, review.version = IN_REVIEW, latest
            review.reviewer, review.requested_by, review.requested_at = reviewer, user, now
            review.decided_by, review.decided_at = None, None
            event.update(reviewer=reviewer, recipient=reviewer)
        elif action == "approve":
            sole = len(team) == 1 and team[0]["id"] == user.id
            allowed = decider if state == IN_REVIEW else sole
            if not allowed:
                if state != IN_REVIEW:
                    raise ReviewStateError(state)
                raise _decide_denied(review)
            review.state, review.version = APPROVED, latest
            review.decided_by, review.decided_at = user, now
            event["recipient"] = _requester(review, user)
        elif action == "request_changes":
            if not decider:
                raise _decide_denied(review)
            if not note:
                raise ReviewInvalid("note", "Say what should change.")
            review.state, review.version = CHANGES, latest
            review.decided_by, review.decided_at = user, now
            event["recipient"] = _requester(review, user)
        elif action == "withdraw":
            if not (decider or review.requested_by_id == user.id):
                raise _decide_denied(review)
            _clear(review)
        else:  # reopen
            _clear(review)
        review.revision = revision + 1
        review.save()
        _add_event(artifact, user, **event)
    return review


def _requester(review, actor):
    """The person who asked for the review, unless they decide it themselves."""
    if review.requested_by_id and review.requested_by_id != actor.id:
        return review.requested_by
    return None


def _clear(review) -> None:
    review.state = DRAFT
    review.reviewer = review.requested_by = review.decided_by = None
    review.requested_at = review.decided_at = None


def _decide_denied(review) -> PermissionDenied:
    who = _name(review.reviewer) if review.reviewer_id else "the reviewer"
    return PermissionDenied(
        f"Only {who}, the project's owner or an organization admin can decide this review.")


def comment(artifact, user, body: str) -> AgentReviewEvent:
    body = _clean_note(body)
    if not body:
        raise ReviewInvalid("body", "Write a comment first.")
    if len(body) > NOTE_MAX_LENGTH:
        raise ReviewInvalid("body", f"Comments are at most {NOTE_MAX_LENGTH:,} characters.")
    latest = (AgentArtifact.objects.filter(pk=artifact.pk)
              .values_list("latest_version", flat=True).first()) or 0
    with transaction.atomic():
        return _add_event(artifact, user, kind=ReviewEventKind.COMMENT.value, version=latest,
                          body=body)


def _add_event(artifact, user, *, kind, version, body="", reviewer=None,
               recipient=None) -> AgentReviewEvent:
    """Append the event; a request or a decision to someone else schedules
    its email once the transaction commits (or records ``off``)."""
    emailed = recipient is not None and kind in (ReviewEventKind.REQUESTED,
                                                 ReviewEventKind.APPROVED,
                                                 ReviewEventKind.CHANGES_REQUESTED)
    ready = emailed and email_ready()
    event = AgentReviewEvent.objects.create(
        artifact_id=artifact.id, website_id=artifact.website_id, actor=user, kind=kind,
        version=version, body=body, reviewer=reviewer, recipient=recipient,
        email="" if not emailed or ready else ReviewEmail.OFF,
    )
    if ready:
        transaction.on_commit(lambda: _send_later(event.id))
    return event


def _send_later(event_id) -> None:
    from apps.agent.tasks import send_review_email

    try:
        send_review_email.delay(str(event_id))
    except Exception:
        logger.warning("agent: the review email %s could not be queued", event_id, exc_info=True)
        AgentReviewEvent.objects.filter(pk=event_id).update(email=ReviewEmail.FAILED)


# ── Email ─────────────────────────────────────────────────────────────────


def email_ready() -> bool:
    """AGENT_REVIEW_EMAILS on, and a mail backend that can send: not the
    dummy backend, and SMTP only with an EMAIL_HOST."""
    from django.conf import settings

    from apps.agent.conf import agent_conf

    if not agent_conf().review_emails:
        return False
    backend = getattr(settings, "EMAIL_BACKEND", "") or ""
    if backend == "django.core.mail.backends.dummy.EmailBackend":
        return False
    if backend == "django.core.mail.backends.smtp.EmailBackend":
        return bool(getattr(settings, "EMAIL_HOST", ""))
    return True


SUBJECTS = {
    ReviewEventKind.REQUESTED.value: '{actor} asked you to review "{title}"',
    ReviewEventKind.APPROVED.value: '{actor} approved "{title}"',
    ReviewEventKind.CHANGES_REQUESTED.value: '{actor} asked for changes to "{title}"',
}


def email_for(event) -> tuple[str, str]:
    """(subject, html) of an event's email: the note and the link to the
    draft in Cansee, never the draft itself."""
    from django.conf import settings
    from django.utils.html import escape

    actor = _name(event.actor) if event.actor_id else "A teammate"
    title = event.artifact.title
    subject = " ".join(SUBJECTS[event.kind].format(actor=actor, title=title).split())[:200]
    link = (f"{str(getattr(settings, 'FRONTEND_URL', '')).rstrip('/')}"
            f"/projects/{event.website_id}/agent/artifacts/{event.artifact_id}")
    lines = [f"<p>{escape(subject)}.</p>"]
    if event.body:
        lines.append(f"<p>{escape(event.body)}</p>".replace("\n", "<br>"))
    lines.append(f'<p><a href="{escape(link)}">{escape(link)}</a></p>')
    return subject, "\n".join(lines)


def send_email(event_id) -> str:
    """Send one event's email; returns the stored ``email`` value."""
    from apps.notifications.services.email_service import EmailService

    event = (AgentReviewEvent.objects.select_related("actor", "recipient", "artifact")
             .filter(pk=event_id).first())
    if event is None or event.email in (ReviewEmail.SENT, ReviewEmail.OFF):
        return event.email if event is not None else ""
    recipient = event.recipient if event.recipient_id else None
    if recipient is None or not recipient.is_active or not recipient.email:
        status = ReviewEmail.FAILED
    else:
        subject, html_content = email_for(event)
        sent = EmailService.send_email(to=recipient.email, subject=subject,
                                       html_content=html_content)
        status = ReviewEmail.SENT if sent else ReviewEmail.FAILED
    AgentReviewEvent.objects.filter(pk=event.pk).update(email=status)
    return status
