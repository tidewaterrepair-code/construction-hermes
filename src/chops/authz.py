"""Server-side authorization. Every service function receives an ``Actor`` and checks it here.

Identity comes only from verified sources: a dashboard session, a hashed API token
(Hermes service identity), the local CLI, or the worker. Message text, usernames and
model output never establish identity or approval.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from .errors import Forbidden
from .models import JobAssignment

# Channels from which an owner decision is accepted as verified.
VERIFIED_DECISION_CHANNELS = {"dashboard", "cli", "hermes_elicitation"}

CAPABILITIES: dict[str, set[str]] = {
    "owner": {
        "read:all", "read:financial", "write:crm", "write:estimate", "write:job", "write:field",
        "write:procurement", "write:billing", "write:documents", "request:approval", "decide:approval",
        "admin:settings", "admin:users", "kill:engage", "kill:release", "payment:verify", "rates:verify",
        "learning:decide", "export:all", "schedule:commit",
    },
    "office": {
        "read:all", "read:financial", "write:crm", "write:estimate", "write:job", "write:field",
        "write:procurement", "write:billing", "write:documents", "request:approval", "kill:engage",
        "payment:verify", "rates:verify", "schedule:commit",
    },
    # The Hermes coordinator: the owner's assistant. It can draft and request; it cannot
    # approve, verify money, release the kill switch, or change policy.
    "agent": {
        "read:all", "read:financial", "write:crm", "write:estimate", "write:job", "write:field",
        "write:procurement", "write:billing", "write:documents", "request:approval", "kill:engage",
    },
    "foreman": {"read:assigned", "write:field", "write:documents"},
    "crew": {"read:assigned", "write:field_photos"},
    "viewer": {"read:all"},
}


@dataclass(frozen=True)
class Actor:
    user_id: int | None
    role: str
    via: str
    display_name: str = ""
    conversation_key: str | None = None
    extra: dict = field(default_factory=dict, compare=False)

    def can(self, capability: str) -> bool:
        return capability in CAPABILITIES.get(self.role, set())

    @property
    def sees_financials(self) -> bool:
        return self.can("read:financial")


SYSTEM_ACTOR = Actor(user_id=None, role="owner", via="system", display_name="system")


def require(actor: Actor, capability: str) -> None:
    if not actor.can(capability):
        raise Forbidden(f"{actor.role} via {actor.via} is not allowed to {capability}")


def require_decider(actor: Actor) -> None:
    require(actor, "decide:approval")
    if actor.via not in VERIFIED_DECISION_CHANNELS:
        raise Forbidden(f"approval decisions are not accepted via {actor.via}")


def job_ids_visible(session: Session, actor: Actor) -> set[int] | None:
    """None means unrestricted; otherwise the explicit set of assigned job ids."""
    if actor.can("read:all"):
        return None
    if actor.user_id is None:
        return set()
    rows = session.scalars(select(JobAssignment.job_id).where(JobAssignment.user_id == actor.user_id))
    return set(rows)


def require_job_access(session: Session, actor: Actor, job_id: int, *, write: bool = False) -> None:
    visible = job_ids_visible(session, actor)
    if visible is not None and job_id not in visible:
        # Same message whether the job exists or not: no cross-job existence probing.
        raise Forbidden("no access to this job")
    if write and not (actor.can("write:job") or actor.can("write:field") or actor.can("write:field_photos")):
        raise Forbidden("read-only access")
