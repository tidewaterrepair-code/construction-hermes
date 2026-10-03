"""Append-only business audit trail."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ..authz import Actor
from ..hashing import jsonable
from ..models import AuditEvent


def record(
    session: Session,
    actor: Actor,
    action: str,
    entity_type: str | None = None,
    entity_id: int | None = None,
    **detail: Any,
) -> None:
    session.add(
        AuditEvent(
            actor_id=actor.user_id,
            actor_role=actor.role,
            via=actor.via,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            detail=jsonable(detail),
        )
    )
