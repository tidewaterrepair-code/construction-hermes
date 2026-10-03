"""UTC storage, America/New_York display. DST handled by zoneinfo."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from .config import get_settings

UTC = dt.timezone.utc


def tz() -> ZoneInfo:
    return ZoneInfo(get_settings().timezone)


def now() -> dt.datetime:
    return dt.datetime.now(UTC)


def today_local() -> dt.date:
    return now().astimezone(tz()).date()


def to_local(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("naive datetime")
    return value.astimezone(tz())


def fmt_local(value: dt.datetime | None) -> str:
    v = to_local(value)
    return "—" if v is None else v.strftime("%a %b %-d, %-I:%M %p %Z")


def parse_local(value: str | dt.datetime) -> dt.datetime:
    """Parse an ISO datetime. Naive values are interpreted in the business timezone.

    Nonexistent local times (spring-forward gap) are rejected; ambiguous fall-back times
    resolve to the first occurrence (fold=0) and are reported by ``is_ambiguous_local``.
    """
    if isinstance(value, dt.datetime):
        d = value
    else:
        d = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if d.tzinfo is None:
        z = tz()
        local = d.replace(tzinfo=z)
        roundtrip = local.astimezone(UTC).astimezone(z)
        if roundtrip.replace(tzinfo=None) != d:
            raise ValueError(f"{d} does not exist in {z.key} (DST gap)")
        d = local
    return d.astimezone(UTC)


def is_ambiguous_local(naive: dt.datetime) -> bool:
    z = tz()
    a = naive.replace(tzinfo=z, fold=0).utcoffset()
    b = naive.replace(tzinfo=z, fold=1).utcoffset()
    return a != b


def iso(value: dt.datetime | dt.date | None) -> str | None:
    return None if value is None else value.isoformat()
