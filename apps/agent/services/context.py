"""A project's context (design/p2_contract.md section 11): six sections of
entries, each with the real sources it comes from, and the documents
people add for the agents.

Shared with the website's members. Entries are edited with optimistic
concurrency (``revision``); an edit based on an older revision is
refused with 409 revision_conflict instead of overwriting a teammate's.
The agents read active entries only (``agent_context``); entries built
from the site wait as suggestions until a person accepts them.

A reference's ``state`` is stored for pages (set when a build re-checks
them) and documents (set when the document is edited); a fact reads
``retired`` and a competitor ``untracked`` when that is true now, which
is computed on every read and never stored.
"""
from __future__ import annotations

from django.db import transaction
from django.db.models import Count, Max, Q

from apps.agent.constants import (
    CONTEXT_DOCUMENT_TITLE_MAX_LENGTH,
    CONTEXT_TITLE_MAX_LENGTH,
    ContextOrigin,
    ContextSection,
    ContextStatus,
    ReferenceKind,
    ReferenceState,
)
from apps.agent.exceptions import RevisionConflict
from apps.agent.models import AgentContextDocument, AgentContextEntry, AgentContextReference
from apps.agent.subagents.sourcing import apex_of, found, is_http, path_label, same_site, water_caps
from apps.agent.tools.shaping import clip
from core.exceptions import CanseeException, ResourceNotFound

# (key, label, help) in display order (contract 11a, exact copy).
SECTIONS: tuple[tuple[str, str, str], ...] = (
    (ContextSection.COMPANY.value, "Company",
     "Who you are: what you do, for whom, since when, and proof such as customers and awards."),
    (ContextSection.AUDIENCE.value, "Audience",
     "Who buys, what they need, what holds them back and how they decide."),
    (ContextSection.COMPETITORS.value, "Competitors",
     "The competitors you track and how you differ from each."),
    (ContextSection.OFFERINGS.value, "Offerings",
     "Products, plans, prices, features and integrations."),
    (ContextSection.STRATEGY.value, "Strategy",
     "What you are trying to win: positioning, priorities, target questions and markets."),
    (ContextSection.EXECUTION.value, "Execution",
     "How to write for you: voice, words to use or avoid, rules and review steps."),
)
SECTION_KEYS = tuple(key for key, _, _ in SECTIONS)
SECTION_LABELS = {key: label for key, label, _ in SECTIONS}

BODY_MAX_LENGTH = 4000
QUOTE_MAX_LENGTH = 500
MAX_ENTRIES_PER_SECTION = 40
MAX_REFERENCES_PER_ENTRY = 10
MAX_DOCUMENTS = 30
DOCUMENT_MAX_CHARS = 50_000
FACT_LABEL_MAX_LENGTH = 160
# What the agents get (contract 11a).
AGENT_CONTEXT_CHARS = 24_000
AGENT_QUOTE_CHARS = 200
# Facts the agents and the evidence may rely on.
USABLE_FACT_STATUSES = ("approved", "auto")
# Why Build from my site is unavailable or limited (contract 11b, exact).
BUILD_REASONS = {
    "insights_disabled": "Building from your site isn't available on this server.",
    "ai_not_configured": "The analysis model isn't configured on this server.",
}


class ContextInvalid(CanseeException):
    """400 ``invalid`` with the field errors in ``details.fields``."""

    def __init__(self, fields: dict):
        message = next(iter(fields.values()), ["Please check your input and try again."])[0]
        super().__init__(message, code="invalid", status_code=400, details={"fields": fields})


class TooMany(CanseeException):
    def __init__(self, message: str):
        super().__init__(message, code="too_many", status_code=400)


# ── Reads ─────────────────────────────────────────────────────────────────


def competitor_list(website) -> list[dict]:
    out = []
    for c in website.competitors or []:
        if isinstance(c, dict) and (c.get("name") or "").strip():
            out.append({"name": c["name"].strip(),
                        "domain": (c.get("domain") or "").strip().lower()})
    return out


def tracked_competitors(website) -> dict[str, str]:
    """Folded domain -> name of each tracked competitor that has a domain."""
    return {fold_domain(c["domain"]): c["name"] for c in competitor_list(website) if c["domain"]}


def fold_domain(domain: str) -> str:
    value = (domain or "").strip().lower()
    if "://" in value:
        value = value.split("://", 1)[1]
    value = value.split("/", 1)[0].strip(".")
    return value[4:] if value.startswith("www.") else value


def fact_label(fact) -> str:
    return clip(f"{fact.subject} {fact.predicate} {fact.object}", FACT_LABEL_MAX_LENGTH)


def _fact_usable(fact) -> bool:
    return fact is not None and fact.version_to is None and fact.status in USABLE_FACT_STATUSES


def reference_state(reference, tracked: dict[str, str] | None = None) -> str:
    """The state shown now: stored for pages and documents, computed for
    facts and competitors."""
    if reference.kind == ReferenceKind.FACT:
        return (ReferenceState.OK.value if _fact_usable(reference.fact)
                else ReferenceState.RETIRED.value)
    if reference.kind == ReferenceKind.COMPETITOR:
        tracked = tracked if tracked is not None else tracked_competitors(reference.website)
        return (ReferenceState.OK.value if fold_domain(reference.competitor_domain) in tracked
                else ReferenceState.UNTRACKED.value)
    return reference.state


def reference_label(reference) -> str:
    if reference.kind == ReferenceKind.PAGE:
        return path_label(reference.url)
    if reference.kind == ReferenceKind.FACT:
        return fact_label(reference.fact) if reference.fact is not None else (reference.title or "")
    if reference.kind == ReferenceKind.DOCUMENT:
        return reference.document.title if reference.document is not None else reference.title
    return reference.competitor_name or reference.competitor_domain


def reference_payload(reference, tracked: dict[str, str] | None = None) -> dict:
    from apps.agent.api.v1.serializers import dt, str_id

    return {
        "id": str(reference.id),
        "kind": reference.kind,
        "label": reference_label(reference),
        "url": reference.url if reference.kind == ReferenceKind.PAGE else "",
        "quote": reference.quote,
        "fact_id": str_id(reference.fact_id),
        "document_id": str_id(reference.document_id),
        "competitor_domain": reference.competitor_domain,
        "state": reference_state(reference, tracked),
        "checked_at": dt(reference.checked_at),
    }


def entry_payload(entry, tracked: dict[str, str] | None = None) -> dict:
    from apps.agent.api.v1.serializers import display_name, dt, str_id

    refs = sorted(entry.references.all(), key=lambda r: (r.created_at, str(r.id)))
    return {
        "id": str(entry.id),
        "section": entry.section,
        "title": entry.title,
        "body": entry.body,
        "status": entry.status,
        "origin": entry.origin,
        "position": entry.position,
        "revision": entry.revision,
        "updated_at": dt(entry.updated_at),
        "updated_by_name": display_name(entry.updated_by) if entry.updated_by_id else None,
        "build_run_id": str_id(entry.build_run_id),
        "references": [reference_payload(r, tracked) for r in refs],
    }


def _entries(website, *, status: str | None = None):
    qs = (AgentContextEntry.objects.filter(website=website)
          .select_related("updated_by")
          .prefetch_related("references__fact", "references__document"))
    if status:
        qs = qs.filter(status=status)
    entries = list(qs)
    order = {key: i for i, key in enumerate(SECTION_KEYS)}
    entries.sort(key=lambda e: (order.get(e.section, len(order)),
                                e.status != ContextStatus.ACTIVE, e.position, e.created_at))
    return entries


def _target(reference) -> tuple:
    if reference.kind == ReferenceKind.PAGE:
        return (reference.kind, reference.url)
    if reference.kind == ReferenceKind.FACT:
        return (reference.kind, str(reference.fact_id))
    if reference.kind == ReferenceKind.DOCUMENT:
        return (reference.kind, str(reference.document_id))
    return (reference.kind, fold_domain(reference.competitor_domain))


def _section_counts(entries) -> tuple[list[dict], dict]:
    per = {key: {"entries": 0, "suggested": 0, "targets": set()} for key in SECTION_KEYS}
    every = set()
    for entry in entries:
        bucket = per.get(entry.section)
        if bucket is None:
            continue
        if entry.status == ContextStatus.SUGGESTED:
            bucket["suggested"] += 1
            continue
        bucket["entries"] += 1
        for reference in entry.references.all():
            bucket["targets"].add(_target(reference))
            every.add(_target(reference))
    sections = [
        {"key": key, "label": label, "help": help_text, "entries": per[key]["entries"],
         "suggested": per[key]["suggested"], "references": len(per[key]["targets"])}
        for key, label, help_text in SECTIONS
    ]
    totals = {
        "entries": sum(s["entries"] for s in sections),
        "suggested": sum(s["suggested"] for s in sections),
        "references": len(every),
    }
    return sections, totals


def fact_counts(website) -> dict:
    from apps.brand_vault.models import BrandFact

    rows = (BrandFact.objects.filter(website=website, version_to__isnull=True)
            .aggregate(approved=Count("id", filter=Q(status__in=USABLE_FACT_STATUSES)),
                       pending=Count("id", filter=Q(status="pending"))))
    return {"approved": rows["approved"] or 0, "pending": rows["pending"] or 0}


def document_payload(document, *, cited_by: int | None = None, text: bool = False) -> dict:
    from apps.agent.api.v1.serializers import display_name, dt

    if cited_by is None:
        cited_by = (AgentContextReference.objects.filter(document=document)
                    .values("entry_id").distinct().count())
    out = {
        "id": str(document.id),
        "title": document.title,
        "chars": document.chars,
        "cited_by": cited_by,
        "updated_at": dt(document.updated_at),
        "updated_by_name": display_name(document.updated_by) if document.updated_by_id else None,
    }
    if text:
        out["text"] = document.text
    return out


def list_documents(website) -> list[dict]:
    documents = (AgentContextDocument.objects.filter(website=website)
                 .select_related("updated_by")
                 .annotate(cited=Count("references__entry", distinct=True))
                 .order_by("-updated_at", "-id"))
    return [document_payload(d, cited_by=d.cited) for d in documents]


def build_block(*, available: bool, reason: str | None, active_run=None,
                last_run=None) -> dict:
    """Contract 11b ``build``; ``reason_message`` carries the reason's text."""
    return {"available": available, "reason": reason,
            "reason_message": BUILD_REASONS.get(reason) if reason else None,
            "active_run": active_run, "last_run": last_run}


def _build_status(website) -> dict:
    """The Build from my site state. The run kind lives in the insights runs
    lifecycle; without that app it is unavailable."""
    from django.apps import apps

    if not apps.is_installed("apps.insights"):
        return build_block(available=False, reason="insights_disabled")
    from apps.agent.services.context_build import build_status

    return build_status(website)


def overview(website, user) -> dict:
    """ContextOverview (contract 11b)."""
    from apps.agent.services.access import can_write

    tracked = tracked_competitors(website)
    entries = _entries(website)
    sections, totals = _section_counts(entries)
    return {
        "sections": sections,
        "entries": [entry_payload(e, tracked) for e in entries],
        "documents": list_documents(website),
        "facts": fact_counts(website),
        "competitors": competitor_list(website),
        "totals": totals,
        "build": _build_status(website),
        "can_edit": can_write(user, website),
    }


def get_entry(website, entry_id) -> AgentContextEntry:
    entry = AgentContextEntry.objects.filter(website=website, pk=entry_id).first()
    if entry is None:
        raise ResourceNotFound("Entry not found.")
    return entry


def get_reference(website, reference_id) -> AgentContextReference:
    reference = (AgentContextReference.objects.select_related("entry")
                 .filter(website=website, pk=reference_id).first())
    if reference is None:
        raise ResourceNotFound("Source not found.")
    return reference


def get_document(website, document_id) -> AgentContextDocument:
    document = (AgentContextDocument.objects.select_related("updated_by")
                .filter(website=website, pk=document_id).first())
    if document is None:
        raise ResourceNotFound("Document not found.")
    return document


def load_entry(entry) -> AgentContextEntry:
    """The entry with what its payload reads."""
    return (AgentContextEntry.objects.select_related("updated_by")
            .prefetch_related("references__fact", "references__document")
            .get(pk=entry.pk))


# ── Entries ───────────────────────────────────────────────────────────────


def _clean_text(fields: dict, *, title, body) -> tuple[str, str]:
    title = (title or "").strip()
    body = (body or "").strip()
    if len(title) > CONTEXT_TITLE_MAX_LENGTH:
        fields["title"] = [f"Titles are at most {CONTEXT_TITLE_MAX_LENGTH} characters."]
    if len(body) > BODY_MAX_LENGTH:
        fields["body"] = [f"Entries are at most {BODY_MAX_LENGTH:,} characters."]
    if not title and not body:
        fields["title"] = ["Write a title or a text."]
    return title, body


def _check_section(section) -> None:
    if section not in SECTION_KEYS:
        raise ContextInvalid({"section": ["Choose one of the six sections."]})


def _check_room(website, section, *, adding: int = 1, exclude=None) -> None:
    qs = AgentContextEntry.objects.filter(website=website, section=section)
    if exclude is not None:
        qs = qs.exclude(pk=exclude.pk)
    if qs.count() + adding > MAX_ENTRIES_PER_SECTION:
        raise TooMany(f"This section has {MAX_ENTRIES_PER_SECTION} entries, the limit.")


def _next_position(website, section, status=ContextStatus.ACTIVE) -> int:
    top = (AgentContextEntry.objects.filter(website=website, section=section, status=status)
           .aggregate(top=Max("position"))["top"])
    return 0 if top is None else top + 1


def create_entry(website, user, *, section, title="", body="", references=(),
                 status=ContextStatus.ACTIVE, origin=ContextOrigin.USER,
                 build_run_id=None) -> AgentContextEntry:
    """A new entry, appended to its section, with its references."""
    _check_section(section)
    fields: dict = {}
    title, body = _clean_text(fields, title=title, body=body)
    references = list(references or [])
    if len(references) > MAX_REFERENCES_PER_ENTRY:
        fields["references"] = [f"An entry has at most {MAX_REFERENCES_PER_ENTRY} sources."]
    if fields:
        raise ContextInvalid(fields)
    with transaction.atomic():
        _check_room(website, section)
        entry = AgentContextEntry.objects.create(
            website=website, section=section, title=title, body=body, status=status,
            origin=origin, position=_next_position(website, section, status), revision=1,
            build_run_id=build_run_id, created_by=user, updated_by=user,
        )
        for data in references:
            _create_reference(entry, data)
    return entry


def update_entry(entry, user, *, revision: int, **fields) -> AgentContextEntry:
    """Change title, body, section or position; ``revision`` must be the
    one the edit started from."""
    with transaction.atomic():
        locked = AgentContextEntry.objects.select_for_update().get(pk=entry.pk)
        if locked.revision != revision:
            raise RevisionConflict(revision=locked.revision)
        errors: dict = {}
        title = fields.get("title", locked.title)
        body = fields.get("body", locked.body)
        title, body = _clean_text(errors, title=title, body=body)
        if errors:
            raise ContextInvalid(errors)
        section = fields.get("section") or locked.section
        _check_section(section)
        if section != locked.section:
            _check_room(locked.website, section, exclude=locked)
            locked.position = _next_position(locked.website, section, locked.status)
            locked.section = section
        if fields.get("position") is not None:
            locked.position = max(0, int(fields["position"]))
        locked.title, locked.body = title, body
        locked.revision = revision + 1
        locked.updated_by = user
        locked.save()
    return locked


def delete_entry(entry, *, revision: int) -> None:
    with transaction.atomic():
        locked = AgentContextEntry.objects.select_for_update().filter(pk=entry.pk).first()
        if locked is None:
            return
        if locked.revision != revision:
            raise RevisionConflict(revision=locked.revision)
        locked.delete()


def review_entries(website, user, *, accept=(), dismiss=()) -> dict:
    """Accept suggestions (they become active at the end of their section)
    or dismiss them (deleted). Ids that are not this project's suggestions
    are left alone."""
    accept = [str(i) for i in accept]
    dismiss = [str(i) for i in dismiss]
    if set(accept) & set(dismiss):
        raise ContextInvalid({"accept": ["A suggestion can be accepted or dismissed, not both."]})
    with transaction.atomic():
        suggested = {
            str(e.id): e for e in AgentContextEntry.objects.select_for_update().filter(
                website=website, status=ContextStatus.SUGGESTED, pk__in=[*accept, *dismiss],
            )
        }
        accepted = 0
        for entry_id in accept:
            entry = suggested.get(entry_id)
            if entry is None:
                continue
            entry.status = ContextStatus.ACTIVE
            entry.position = _next_position(website, entry.section)
            entry.revision += 1
            entry.updated_by = user
            entry.save()
            accepted += 1
        gone = [entry_id for entry_id in dismiss if entry_id in suggested]
        AgentContextEntry.objects.filter(pk__in=gone).delete()
    return {"accepted": accepted, "dismissed": len(gone)}


# ── References ────────────────────────────────────────────────────────────


def _reference_values(entry, data: dict) -> dict:
    """The fields of a new reference, validated per kind (contract 11a)."""
    from apps.brand_vault.models import BrandFact

    website = entry.website
    kind = (data.get("kind") or "").strip()
    quote = (data.get("quote") or "").strip()
    if len(quote) > QUOTE_MAX_LENGTH:
        raise ContextInvalid({"quote": [f"Quotes are at most {QUOTE_MAX_LENGTH} characters."]})
    if kind == ReferenceKind.PAGE:
        url = (data.get("url") or "").strip()
        if not is_http(url) or len(url) > 2000:
            raise ContextInvalid({"url": ["Enter a full http(s) address."]})
        if not same_site(url, website):
            raise ContextInvalid({"url": [f"Use pages of {apex_of(website.url or '')} only."]})
        return {"kind": kind, "url": url, "quote": quote, "state": ReferenceState.UNCHECKED}
    if kind == ReferenceKind.FACT:
        fact = (BrandFact.objects.filter(website=website, pk=data.get("fact_id"),
                                         version_to__isnull=True,
                                         status__in=USABLE_FACT_STATUSES).first()
                if is_uuid(data.get("fact_id")) else None)
        if fact is None:
            raise ContextInvalid({"fact_id": ["Choose an approved brand fact of this project."]})
        return {"kind": kind, "fact": fact, "title": fact_label(fact)[:300],
                "state": ReferenceState.OK}
    if kind == ReferenceKind.DOCUMENT:
        document = (AgentContextDocument.objects.filter(website=website,
                                                        pk=data.get("document_id")).first()
                    if is_uuid(data.get("document_id")) else None)
        if document is None:
            raise ContextInvalid({"document_id": ["Choose a document of this project."]})
        if quote and not found(document.text, quote):
            raise ContextInvalid({"quote": ["These words aren't in the document."]})
        return {"kind": kind, "document": document, "title": document.title[:300], "quote": quote,
                "state": ReferenceState.OK}
    if kind == ReferenceKind.COMPETITOR:
        domain = fold_domain(data.get("competitor_domain") or "")
        tracked = tracked_competitors(website)
        if not domain or domain not in tracked:
            raise ContextInvalid({"competitor_domain": [
                "Choose a competitor this project tracks."]})
        return {"kind": kind, "competitor_domain": domain, "competitor_name": tracked[domain][:200],
                "state": ReferenceState.OK}
    raise ContextInvalid({"kind": ["Choose page, fact, document or competitor."]})


def is_uuid(value) -> bool:
    import uuid

    try:
        uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return False
    return True


def _create_reference(entry, data: dict, *, state: str | None = None,
                      checked_at=None) -> AgentContextReference:
    values = _reference_values(entry, data)
    if state is not None:
        values["state"] = state
    return AgentContextReference.objects.create(entry=entry, website_id=entry.website_id,
                                                checked_at=checked_at, **values)


def add_reference(entry, user, data: dict) -> AgentContextReference:
    with transaction.atomic():
        locked = AgentContextEntry.objects.select_for_update().get(pk=entry.pk)
        if locked.references.count() >= MAX_REFERENCES_PER_ENTRY:
            raise TooMany(f"This entry has {MAX_REFERENCES_PER_ENTRY} sources, the limit.")
        reference = _create_reference(locked, data)
        # Not a revision bump: an open edit of the entry's text stays valid.
        locked.updated_by = user
        locked.save(update_fields=["updated_by", "updated_at"])
    return reference


def delete_reference(reference) -> None:
    reference.delete()


# ── Documents ─────────────────────────────────────────────────────────────


def _clean_document(*, title, text, partial: bool) -> tuple[str | None, str | None]:
    fields: dict = {}
    if title is not None or not partial:
        title = (title or "").strip()
        if not title:
            fields["title"] = ["Give the document a title."]
        elif len(title) > CONTEXT_DOCUMENT_TITLE_MAX_LENGTH:
            fields["title"] = [f"Titles are at most {CONTEXT_DOCUMENT_TITLE_MAX_LENGTH} "
                               "characters."]
    if text is not None or not partial:
        text = (text or "").replace("\x00", "").strip()
        if not text:
            fields["text"] = ["Paste or write the document's text."]
        elif len(text) > DOCUMENT_MAX_CHARS:
            fields["text"] = [f"Documents are at most {DOCUMENT_MAX_CHARS:,} characters."]
    if fields:
        raise ContextInvalid(fields)
    return title, text


def create_document(website, user, *, title, text) -> AgentContextDocument:
    title, text = _clean_document(title=title, text=text, partial=False)
    with transaction.atomic():
        if AgentContextDocument.objects.filter(website=website).count() >= MAX_DOCUMENTS:
            raise TooMany(f"This project has {MAX_DOCUMENTS} documents, the limit.")
        return AgentContextDocument.objects.create(
            website=website, title=title, text=text, chars=len(text), created_by=user,
            updated_by=user,
        )


def update_document(document, user, *, title=None, text=None) -> AgentContextDocument:
    """Rename or rewrite a document. A rewrite re-checks the quotes that
    cite it: one no longer in the text reads ``changed``."""
    title, text = _clean_document(title=title, text=text, partial=True)
    with transaction.atomic():
        locked = AgentContextDocument.objects.select_for_update().get(pk=document.pk)
        if title is not None:
            locked.title = title
        if text is not None:
            locked.text = text
            locked.chars = len(text)
        locked.updated_by = user
        locked.save()
        if text is not None:
            quoted = AgentContextReference.objects.filter(document=locked).exclude(quote="")
            for reference in quoted:
                state = (ReferenceState.OK.value if found(text, reference.quote)
                         else ReferenceState.CHANGED.value)
                if reference.state != state:
                    reference.state = state
                    reference.save(update_fields=["state"])
        if title is not None:
            AgentContextReference.objects.filter(document=locked).update(title=title[:300])
    return locked


def delete_document(document) -> None:
    """The references citing it go with it; their entries stay."""
    document.delete()


# ── What the agents and the insights read ───────────────────────────────


def _agent_references(entry, tracked) -> list[dict]:
    out = []
    for reference in sorted(entry.references.all(), key=lambda r: (r.created_at, str(r.id))):
        if reference_state(reference, tracked) in (ReferenceState.RETIRED,
                                                   ReferenceState.UNTRACKED):
            continue
        out.append({
            "kind": reference.kind,
            "label": reference_label(reference),
            "url": reference.url if reference.kind == ReferenceKind.PAGE else "",
            "quote": clip(reference.quote, AGENT_QUOTE_CHARS) if reference.quote else "",
        })
    return out


def _refs_size(refs: list[dict]) -> int:
    return sum(len(r["label"]) + len(r["url"]) + len(r["quote"]) for r in refs)


def agent_context(website, *, max_chars: int = AGENT_CONTEXT_CHARS) -> dict:
    """``{section: [{"id": "e{n}", "title", "body", "references"}]}`` for
    every section, active entries only, in section order.

    Titles, bodies and references fit in ``max_chars`` together: the
    sections share it the way page texts share a request (short sections
    whole, long ones an equal share), and inside a section that is over
    its share the bodies are cut the same way.
    """
    tracked = tracked_competitors(website)
    grouped: dict[str, list] = {key: [] for key in SECTION_KEYS}
    for entry in _entries(website, status=ContextStatus.ACTIVE):
        if entry.section in grouped:
            grouped[entry.section].append((entry, _agent_references(entry, tracked)))
    sizes = [sum(len(e.title) + len(e.body) + _refs_size(refs) for e, refs in grouped[key])
             for key in SECTION_KEYS]
    caps = dict(zip(SECTION_KEYS, water_caps(sizes, max_chars), strict=True))
    out: dict[str, list] = {}
    n = 0
    for key, size in zip(SECTION_KEYS, sizes, strict=True):
        items = grouped[key]
        bodies = [e.body for e, _ in items]
        if size > caps[key]:
            fixed = sum(len(e.title) + _refs_size(refs) for e, refs in items)
            room = caps[key] - fixed
            if room < 0:
                # Titles and sources alone are over the share: keep the
                # leading entries that fit, without their texts.
                kept, used = [], 0
                for entry, refs in items:
                    used += len(entry.title) + _refs_size(refs)
                    if used > caps[key]:
                        break
                    kept.append((entry, refs))
                items, bodies = kept, [""] * len(kept)
            else:
                bodies = [body[:cap] for body, cap in
                          zip(bodies, water_caps([len(b) for b in bodies], room), strict=True)]
        section = []
        for (entry, refs), body in zip(items, bodies, strict=True):
            n += 1
            section.append({"id": f"e{n}", "title": entry.title, "body": body,
                            "references": refs})
        out[key] = section
    return out


def offering_urls(website) -> list[str]:
    """Same-site page references of active Offerings entries, in order."""
    urls: list[str] = []
    for entry in _entries(website, status=ContextStatus.ACTIVE):
        if entry.section != ContextSection.OFFERINGS:
            continue
        for reference in sorted(entry.references.all(), key=lambda r: (r.created_at, str(r.id))):
            if (reference.kind == ReferenceKind.PAGE and reference.url not in urls
                    and same_site(reference.url, website)):
                urls.append(reference.url)
    return urls


def section_text(website, section) -> str:
    """Active entries of one section as "title: body" lines."""
    lines = []
    qs = (AgentContextEntry.objects.filter(website=website, section=section,
                                           status=ContextStatus.ACTIVE)
          .order_by("position", "created_at"))
    for title, body in qs.values_list("title", "body"):
        title, body = (title or "").strip(), " ".join((body or "").split())
        lines.append(f"{title}: {body}" if title and body else title or body)
    return "\n".join(line for line in lines if line)


def counts(website) -> dict[str, int]:
    """Active entries per section, every section present."""
    out = dict.fromkeys(SECTION_KEYS, 0)
    rows = (AgentContextEntry.objects.filter(website=website, status=ContextStatus.ACTIVE)
            .values("section").annotate(n=Count("id")))
    for row in rows:
        if row["section"] in out:
            out[row["section"]] = row["n"]
    return out


def suggested_count(website) -> int:
    return AgentContextEntry.objects.filter(website=website,
                                            status=ContextStatus.SUGGESTED).count()


# ── What a build reads and writes (services.context_build) ──────────────


def url_key(url: str) -> str:
    """A page address compared loosely: host lower-cased without www.,
    fragment and trailing slash dropped."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return url or ""
    host = (parts.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    path = parts.path.rstrip("/")
    return f"{host}{path}?{parts.query}" if parts.query else f"{host}{path}"


def cited_pages(website, *, limit: int) -> list[str]:
    """Pages cited by active entries, newest reference first, one per address."""
    urls, seen = [], set()
    refs = (AgentContextReference.objects
            .filter(website=website, kind=ReferenceKind.PAGE,
                    entry__status=ContextStatus.ACTIVE)
            .exclude(url="").order_by("-created_at").values_list("url", flat=True))
    for url in refs:
        key = url_key(url)
        if key not in seen:
            seen.add(key)
            urls.append(url)
        if len(urls) >= limit:
            break
    return urls


def recheck_pages(website, pages: dict) -> dict[str, int]:
    """Store what a build found on the pages active entries cite.
    ``pages`` maps url_key(address) to the SourcePage read there (its full
    text). A page that did not load reads ``failed``; one whose quote is
    gone, ``changed``."""
    from django.utils import timezone

    tally = {"ok": 0, "changed": 0, "failed": 0}
    now = timezone.now()
    refs = AgentContextReference.objects.filter(website=website, kind=ReferenceKind.PAGE,
                                                entry__status=ContextStatus.ACTIVE)
    for reference in refs:
        page = pages.get(url_key(reference.url))
        if page is None:
            continue
        if not page.ok:
            state = ReferenceState.FAILED.value
        elif reference.quote and not found(page.text, reference.quote):
            state = ReferenceState.CHANGED.value
        else:
            state = ReferenceState.OK.value
        reference.state, reference.checked_at = state, now
        reference.save(update_fields=["state", "checked_at"])
        tally[state] += 1
    return tally


def existing_titles(website) -> dict[str, list[str]]:
    """Titles of the active entries per section (a build's <existing>)."""
    out: dict[str, list[str]] = {key: [] for key in SECTION_KEYS}
    rows = (AgentContextEntry.objects.filter(website=website, status=ContextStatus.ACTIVE)
            .order_by("position", "created_at").values_list("section", "title"))
    for section, title in rows:
        if section in out and title:
            out[section].append(title)
    return out


def replace_suggestions(website, user, *, run_id, sections, drafts) -> dict[str, int]:
    """In one transaction: the never-accepted suggestions of ``sections``
    go, and the build's ``drafts`` ({section, title, body, references:
    [field values]}) are saved as suggestions, as far as each section has
    room. Returns how many were saved per section."""
    saved: dict[str, int] = {}
    with transaction.atomic():
        AgentContextEntry.objects.filter(website=website, section__in=list(sections),
                                         status=ContextStatus.SUGGESTED).delete()
        used = dict(AgentContextEntry.objects.filter(website=website, section__in=list(sections))
                    .values_list("section").annotate(n=Count("id")))
        for draft in drafts:
            section = draft["section"]
            if used.get(section, 0) >= MAX_ENTRIES_PER_SECTION:
                continue
            entry = AgentContextEntry.objects.create(
                website=website, section=section, title=draft["title"], body=draft["body"],
                status=ContextStatus.SUGGESTED, origin=ContextOrigin.BUILT,
                position=_next_position(website, section, ContextStatus.SUGGESTED), revision=1,
                build_run_id=run_id, created_by=user, updated_by=user,
            )
            for values in draft["references"]:
                AgentContextReference.objects.create(entry=entry, website=website, **values)
            used[section] = used.get(section, 0) + 1
            saved[section] = saved.get(section, 0) + 1
    return saved


def offerings_for_writers(website) -> list[dict]:
    """``[{name, description, url}]`` from active Offerings entries: the
    first same-site page each cites, or ""."""
    out = []
    for entry in _entries(website, status=ContextStatus.ACTIVE):
        if entry.section != ContextSection.OFFERINGS:
            continue
        url = next((r.url for r in sorted(entry.references.all(), key=lambda r: r.created_at)
                    if r.kind == ReferenceKind.PAGE and same_site(r.url, website)), "")
        out.append({"name": entry.title, "description": entry.body, "url": url})
    return out
