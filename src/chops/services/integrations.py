"""Integration adapters and their honest status.

Every adapter reports connected / disconnected / degraded with last success/error, plus a
verification level: none | contract_test | live_read | live_delivery. A mocked contract test
never upgrades verification to live.
"""

from __future__ import annotations

import datetime as dt
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import timeutil
from ..config import get_settings
from ..models import Contact, Document, Integration
from . import documents


class TransientError(Exception):
    def __init__(self, msg: str, retry_after: dt.timedelta | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


class PermanentError(Exception):
    pass


class AmbiguousError(Exception):
    """The request may or may not have been accepted by the provider (e.g. read timeout)."""


def check_outbound_url(url: str) -> None:
    """SSRF guard: only https to explicitly allowlisted hosts."""
    u = urlparse(url)
    allowed = {h.strip().lower() for h in get_settings().outbound_hosts.split(",") if h.strip()}
    if u.scheme != "https" or (u.hostname or "").lower() not in allowed:
        raise PermanentError(f"outbound destination not allowlisted: {u.hostname}")


class Adapter:
    name = "base"
    kind = "base"
    idempotent = False  # provider-side idempotency key support

    def configured(self) -> bool:
        return False

    def is_connected(self, session: Session) -> bool:
        if not self.configured():
            return False
        row = session.get(Integration, self.name)
        return row is not None and row.status in ("connected", "degraded")

    def send(self, session: Session, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        raise NotImplementedError


class EmailAdapter(Adapter):
    name = "email_smtp"
    kind = "email"

    def configured(self) -> bool:
        s = get_settings()
        return bool(s.smtp_host and s.smtp_from and s.smtp_user and s.smtp_password)

    def send(self, session: Session, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        s = get_settings()
        msg = EmailMessage()
        msg["From"] = s.smtp_from
        msg["To"] = payload["to"]
        msg["Subject"] = payload["subject"]
        msg["Message-ID"] = f"<{idempotency_key}@chops>"
        msg.set_content(payload["body"])
        for doc_id in payload.get("attachments", []):
            d = session.get(Document, doc_id)
            data = documents.storage_path(d.sha256).read_bytes()
            maintype, subtype = d.mime_type.split("/", 1)
            msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=d.original_filename)
        try:
            with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                smtp.login(s.smtp_user, s.smtp_password.get_secret_value())
                refused = smtp.send_message(msg)
        except (smtplib.SMTPServerDisconnected, TimeoutError) as exc:
            raise AmbiguousError(f"SMTP connection lost; message may have been accepted: {exc}") from exc
        except smtplib.SMTPResponseException as exc:
            if 400 <= exc.smtp_code < 500:
                raise TransientError(f"SMTP {exc.smtp_code}") from exc
            raise PermanentError(f"SMTP {exc.smtp_code}: {exc.smtp_error!r}") from exc
        except OSError as exc:
            raise TransientError(f"SMTP connect failed: {exc}") from exc
        if refused:
            raise PermanentError(f"recipients refused: {list(refused)}")
        return {"provider_ref": msg["Message-ID"]}


class TelegramNotifyAdapter(Adapter):
    """Owner notifications only (digests/alerts) to the configured owner chat id."""

    name = "telegram_notify"
    kind = "telegram"

    def configured(self) -> bool:
        s = get_settings()
        return bool(s.telegram_bot_token and s.telegram_owner_chat_id)

    def send(self, session: Session, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        s = get_settings()
        url = f"https://api.telegram.org/bot{s.telegram_bot_token.get_secret_value()}/sendMessage"
        check_outbound_url(url)
        try:
            r = httpx.post(url, json={"chat_id": s.telegram_owner_chat_id, "text": payload["text"][:4000],
                                      "disable_web_page_preview": True}, timeout=httpx.Timeout(10, read=20))
        except httpx.ConnectError as exc:
            raise TransientError("telegram connect failed") from exc
        except httpx.TimeoutException as exc:
            raise AmbiguousError("telegram timed out after sending; delivery unknown") from exc
        if r.status_code == 429:
            retry = int(r.json().get("parameters", {}).get("retry_after", 30))
            raise TransientError("telegram rate limited", retry_after=dt.timedelta(seconds=retry))
        if r.status_code >= 500:
            raise TransientError(f"telegram {r.status_code}")
        if r.status_code >= 400:
            raise PermanentError(f"telegram {r.status_code}")
        return {"provider_ref": str(r.json().get("result", {}).get("message_id"))}


class SmsAdapter(Adapter):
    name = "sms"
    kind = "sms"

    def configured(self) -> bool:
        return False  # no SMS provider configured in this deployment


class InternalNotifyAdapter(Adapter):
    name = "dashboard_inbox"
    kind = "internal"
    idempotent = True

    def configured(self) -> bool:
        return True

    def is_connected(self, session: Session) -> bool:
        return True

    def send(self, session: Session, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        from ..models import Notification

        if session.scalar(select(Notification).where(Notification.fingerprint == idempotency_key[:64])) is None:
            session.add(Notification(kind=payload.get("kind", "info"), title=payload["title"][:200], body=payload["body"],
                                     fingerprint=idempotency_key[:64]))
        return {"provider_ref": "inbox"}


ADAPTERS: dict[str, Adapter] = {
    "email.send": EmailAdapter(),
    "telegram.notify": TelegramNotifyAdapter(),
    "sms.send": SmsAdapter(),
    "inbox.notify": InternalNotifyAdapter(),
}

# Status rows for integrations that are not outbound adapters (shown on the dashboard).
CATALOG = {
    "email_smtp": ("email", "Customer email via SMTP"),
    "telegram_notify": ("messaging", "Owner alerts via Telegram bot"),
    "sms": ("messaging", "SMS provider"),
    "hermes_gateway": ("agent", "Hermes Agent coordinator (MCP client)"),
    "model_provider": ("agent", "LLM provider for Hermes"),
    "accounting": ("accounting", "Accounting system (QuickBooks/Xero/...)"),
    "calendar": ("calendar", "Calendar sync"),
    "transcription": ("ai", "Voice-note transcription"),
    "weather": ("data", "Weather advisories"),
    "offsite_backup": ("backup", "Off-server backup destination"),
}


def adapter_for(kind: str) -> Adapter | None:
    return ADAPTERS.get(kind)


def ensure_rows(session: Session) -> None:
    for name, (kind, detail) in CATALOG.items():
        if session.get(Integration, name) is None:
            session.add(Integration(name=name, kind=kind, status="disconnected", detail=detail))
    session.flush()


def refresh_configured(session: Session) -> None:
    """Credentials present -> connected (contract level) unless already degraded; absent -> disconnected."""
    ensure_rows(session)
    for adapter in ADAPTERS.values():
        row = session.get(Integration, adapter.name)
        if row is None:
            continue
        if adapter.configured():
            if row.status == "disconnected":
                row.status = "connected"
        else:
            row.status = "disconnected"
            row.verification = "none"


def mark_success(session: Session, name: str, verification: str | None = None) -> None:
    row = session.get(Integration, name)
    if row is not None:
        row.status = "connected"
        row.last_success_at = timeutil.now()
        if verification:
            row.verification = verification


def mark_error(session: Session, name: str, err: str, degraded: bool) -> None:
    row = session.get(Integration, name)
    if row is not None:
        row.last_error = err[:500]
        row.last_error_at = timeutil.now()
        if degraded:
            row.status = "degraded"


def set_status(session: Session, name: str, status: str, detail: str | None = None, verification: str | None = None) -> None:
    ensure_rows(session)
    row = session.get(Integration, name)
    row.status = status
    if detail:
        row.detail = detail
    if verification:
        row.verification = verification
    if status == "connected":
        row.last_success_at = timeutil.now()


def status_list(session: Session) -> list[dict[str, Any]]:
    ensure_rows(session)
    return [{"name": r.name, "kind": r.kind, "status": r.status, "verification": r.verification, "detail": r.detail,
             "last_success_at": timeutil.iso(r.last_success_at), "last_error": r.last_error,
             "last_error_at": timeutil.iso(r.last_error_at)}
            for r in session.scalars(select(Integration).order_by(Integration.kind, Integration.name))]


def contact_email(session: Session, contact_id: int) -> str | None:
    c = session.get(Contact, contact_id)
    return c.email if c else None
