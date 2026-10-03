"""Stable, human-readable record references (e.g. JOB-12, EST-4, PROP-9)."""

from __future__ import annotations

import re

from .errors import ValidationFailed

PREFIX = {
    "lead": "LEAD", "contact": "CON", "site": "SITE", "estimate": "EST", "estimate_revision": "REV",
    "proposal": "PROP", "approval": "APR", "job": "JOB", "task": "TSK", "daily_log": "LOG",
    "change_order": "CO", "purchase_order": "PO", "vendor_quote": "VQ", "cost": "CST", "invoice": "INV",
    "payment": "PAY", "permit": "PRM", "inspection": "INSP", "document": "DOC", "rate": "RATE",
    "outbox": "OBX", "crew": "CREW", "rfi": "RFI", "punch": "PUN", "appointment": "APPT",
    "vendor_document": "VDOC", "research": "SRC", "assembly": "ASM", "user": "USR",
}
_BY_PREFIX = {v: k for k, v in PREFIX.items()}
_RX = re.compile(r"^\s*([A-Za-z]+)-?(\d+)\s*$")


def ref(kind: str, id_: int | None) -> str | None:
    if id_ is None:
        return None
    return f"{PREFIX[kind]}-{id_}"


def parse(value: str | int, expected: str) -> int:
    """Accept 'JOB-12', 'job12', or 12. Rejects a reference of another kind."""
    if isinstance(value, int):
        return value
    s = str(value).strip()
    if s.isdigit():
        return int(s)
    m = _RX.match(s)
    if not m:
        raise ValidationFailed(f"not a valid {expected} reference: {value!r}")
    kind = _BY_PREFIX.get(m.group(1).upper())
    if kind != expected:
        raise ValidationFailed(f"{value!r} is not a {expected} reference (expected {PREFIX[expected]}-<n>)")
    return int(m.group(2))
