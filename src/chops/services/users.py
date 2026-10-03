"""Users, service tokens, and dashboard sessions."""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..authz import Actor, require
from ..config import get_settings
from ..errors import Forbidden, ValidationFailed
from ..models import ROLES, ApiToken, RateLimitHit, User, WebSession
from . import audit

_ph = PasswordHasher()


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def create_user(
    session: Session,
    actor: Actor | None,
    *,
    username: str,
    display_name: str,
    role: str,
    password: str | None = None,
    telegram_user_id: str | None = None,
    bootstrap: bool = False,
) -> User:
    if role not in ROLES:
        raise ValidationFailed(f"role must be one of {ROLES}")
    if bootstrap:
        # Only allowed when no owner exists yet (first-run from the local CLI).
        if session.scalar(select(func.count()).select_from(User).where(User.role == "owner")):
            raise Forbidden("an owner already exists; bootstrap refused")
        if role != "owner":
            raise ValidationFailed("bootstrap creates the owner only")
    else:
        if actor is None:
            raise Forbidden("actor required")
        require(actor, "admin:users")
    if password is not None and len(password) < 12:
        raise ValidationFailed("password must be at least 12 characters")
    u = User(
        username=username.strip().lower(),
        display_name=display_name.strip(),
        role=role,
        password_hash=_ph.hash(password) if password else None,
        telegram_user_id=telegram_user_id,
    )
    session.add(u)
    session.flush()
    audit.record(session, actor or Actor(None, "owner", "cli", "bootstrap"), "user.create", "user", u.id,
                 username=u.username, role=role)
    return u


def set_password(session: Session, actor: Actor, user_id: int, password: str) -> None:
    if actor.user_id != user_id:
        require(actor, "admin:users")
    if len(password) < 12:
        raise ValidationFailed("password must be at least 12 characters")
    u = session.get(User, user_id)
    if u is None:
        raise ValidationFailed("no such user")
    u.password_hash = _ph.hash(password)
    session.execute(delete(WebSession).where(WebSession.user_id == user_id))
    audit.record(session, actor, "user.password_set", "user", user_id)


def authenticate(session: Session, username: str, password: str) -> User | None:
    u = session.scalar(select(User).where(User.username == username.strip().lower(), User.active.is_(True)))
    if u is None or not u.password_hash:
        _ph.hash("timing-equalizer")
        return None
    try:
        _ph.verify(u.password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return None
    return u


# ------------------------------------------------------------------ API tokens (service identities)


def issue_token(session: Session, actor: Actor | None, user_id: int, name: str, days: int | None = 365) -> str:
    if actor is not None:
        require(actor, "admin:users")
    raw = "chops_" + secrets.token_urlsafe(32)
    session.add(ApiToken(
        user_id=user_id, name=name, token_hash=sha256(raw),
        expires_at=(_now() + dt.timedelta(days=days)) if days else None,
    ))
    session.flush()
    audit.record(session, actor or Actor(None, "owner", "cli", "cli"), "token.issue", "user", user_id, name=name)
    return raw


def resolve_token(session: Session, raw: str, via: str = "mcp") -> Actor | None:
    if not raw or not raw.startswith("chops_"):
        return None
    tok = session.scalar(select(ApiToken).where(ApiToken.token_hash == sha256(raw)))
    if tok is None or tok.revoked_at is not None:
        return None
    if tok.expires_at is not None and tok.expires_at < _now():
        return None
    u = tok.user
    if not u.active:
        return None
    tok.last_used_at = _now()
    return Actor(user_id=u.id, role=u.role, via=via, display_name=u.display_name)


def revoke_tokens(session: Session, actor: Actor | None, user_id: int) -> int:
    if actor is not None:
        require(actor, "admin:users")
    n = 0
    for tok in session.scalars(select(ApiToken).where(ApiToken.user_id == user_id, ApiToken.revoked_at.is_(None))):
        tok.revoked_at = _now()
        n += 1
    return n


# ------------------------------------------------------------------ dashboard sessions


def create_web_session(session: Session, user: User, user_agent: str | None) -> tuple[str, str]:
    raw = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    session.add(WebSession(
        id_hash=sha256(raw), user_id=user.id, csrf_token=csrf, user_agent=(user_agent or "")[:300],
        expires_at=_now() + dt.timedelta(hours=get_settings().session_hours),
    ))
    return raw, csrf


def resolve_web_session(session: Session, raw: str | None) -> tuple[Actor, WebSession] | None:
    if not raw:
        return None
    ws = session.get(WebSession, sha256(raw))
    if ws is None or ws.expires_at < _now() or not ws.user.active:
        return None
    u = ws.user
    return Actor(user_id=u.id, role=u.role, via="dashboard", display_name=u.display_name), ws


def end_web_session(session: Session, raw: str | None) -> None:
    if raw:
        session.execute(delete(WebSession).where(WebSession.id_hash == sha256(raw)))


def rate_limited(session: Session, bucket: str, limit: int, window_seconds: int = 60) -> bool:
    """Record a hit and report whether the bucket exceeded ``limit`` within the window."""
    since = _now() - dt.timedelta(seconds=window_seconds)
    session.execute(delete(RateLimitHit).where(RateLimitHit.at < _now() - dt.timedelta(hours=1)))
    count = session.scalar(select(func.count()).select_from(RateLimitHit).where(
        RateLimitHit.bucket == bucket, RateLimitHit.at >= since))
    session.add(RateLimitHit(bucket=bucket))
    return (count or 0) >= limit
