# One ladder for both role vocabularies: the org layer says "member"
# (core.utils.constants.OrgRole), dormant WebsiteMembership rows say
# "editor" — they are the same read-write rank. Before this alias existed,
# ROLE_HIERARCHY.get("member", 0) scored an org member at 0, BELOW viewer.
ROLE_HIERARCHY = {
    "owner": 100,
    "admin": 80,
    "member": 60,
    "editor": 60,  # legacy alias of member
    "viewer": 20,
}


def role_at_least(role: str, required: str) -> bool:
    """True when ``role`` meets or outranks ``required`` on the ladder.

    Unknown strings rank 0 on either side, so an unknown *held* role is
    denied and an unknown *required* role would allow anyone — always pass
    ``required`` from OrgRole/UserRole values.
    """
    return ROLE_HIERARCHY.get(role, 0) >= ROLE_HIERARCHY.get(required, 0)


#: Methods that only read. Viewers may make these; anything else changes
#: data and needs member or above (see TenantScopedAPIView.get_website).
READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def min_role_for_method(method: str) -> str:
    """The least org role a request with this HTTP method needs."""
    return "viewer" if (method or "").upper() in READ_METHODS else "member"


def denied_message(required: str) -> str:
    """What a current org member below ``required`` is told (403)."""
    if ROLE_HIERARCHY.get(required, 0) > ROLE_HIERARCHY["member"]:
        return "Only organization admins can do this."
    return "Your organization role can view this project but not change it."
