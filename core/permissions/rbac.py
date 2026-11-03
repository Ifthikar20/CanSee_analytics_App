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
