"""Canonical JSON and content hashes for immutable snapshots and approval payloads."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from decimal import Decimal
from typing import Any


def _default(o: Any) -> Any:
    if isinstance(o, Decimal):
        # Normalize so 10.0 and 10.00 hash identically.
        return format(o.normalize(), "f") if o == o.to_integral_value() else format(o.normalize(), "f")
    if isinstance(o, (dt.datetime, dt.date)):
        return o.isoformat()
    raise TypeError(f"not serializable: {type(o).__name__}")


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_default, ensure_ascii=False)


def content_hash(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def jsonable(obj: Any) -> Any:
    """Round-trip through canonical JSON so JSONB columns never see Decimal/datetime objects."""
    return json.loads(canonical_json(obj))
