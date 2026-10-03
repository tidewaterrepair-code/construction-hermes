"""Money arithmetic. All business totals come from here, never from model output.

Definitions (used consistently everywhere):
- markup:       price = cost * (1 + markup)
- target margin: price = cost / (1 - margin), 0 <= margin < 1
- gross margin: (revenue - job cost) / revenue; undefined (None) when revenue is 0
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Iterable

CENT = Decimal("0.01")
ZERO = Decimal("0")
ONE = Decimal("1")


class MoneyError(ValueError):
    pass


def D(value: object) -> Decimal:
    """Strict Decimal conversion. Floats are rejected to avoid binary rounding surprises."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        raise MoneyError("float values are not accepted for money/quantities; pass a string or Decimal")
    if isinstance(value, bool):
        raise MoneyError("boolean is not a number")
    try:
        return Decimal(str(value).strip().replace(",", "").replace("$", ""))
    except (InvalidOperation, AttributeError) as exc:
        raise MoneyError(f"not a number: {value!r}") from exc


def q2(value: Decimal) -> Decimal:
    """Round to cents, half-up (customary for invoices)."""
    return D(value).quantize(CENT, rounding=ROUND_HALF_UP)


def pct(value: object) -> Decimal:
    """Accept 0.3, '0.3', '30%', or 30 (>1 treated as percent) and return a fraction."""
    if isinstance(value, str) and value.strip().endswith("%"):
        return D(value.strip()[:-1]) / 100
    d = D(value)
    if d > 1:
        return d / 100
    return d


def price_with_markup(cost: Decimal, markup: Decimal) -> Decimal:
    cost, markup = D(cost), D(markup)
    if markup < 0:
        raise MoneyError("markup cannot be negative")
    return q2(cost * (ONE + markup))


def price_for_margin(cost: Decimal, margin: Decimal) -> Decimal:
    cost, margin = D(cost), D(margin)
    if not (ZERO <= margin < ONE):
        raise MoneyError("target gross margin must satisfy 0 <= margin < 1")
    return q2(cost / (ONE - margin))


def gross_margin(revenue: Decimal, cost: Decimal) -> Decimal | None:
    revenue, cost = D(revenue), D(cost)
    if revenue == 0:
        return None
    return ((revenue - cost) / revenue).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def markup_equivalent_of_margin(margin: Decimal) -> Decimal:
    margin = D(margin)
    if not (ZERO <= margin < ONE):
        raise MoneyError("margin must satisfy 0 <= margin < 1")
    return margin / (ONE - margin)


def allocate(total: Decimal, weights: Iterable[Decimal]) -> list[Decimal]:
    """Split ``total`` proportionally to ``weights`` in cents; remainder goes to the largest weight.

    Guarantees sum(result) == q2(total).
    """
    total = q2(total)
    ws = [D(w) for w in weights]
    if not ws:
        return []
    wsum = sum(ws, ZERO)
    if wsum == 0:
        out = [ZERO] * len(ws)
        out[-1] = total
        return out
    out = [q2(total * w / wsum) for w in ws]
    diff = total - sum(out, ZERO)
    if diff:
        idx = max(range(len(ws)), key=lambda i: ws[i])
        out[idx] += diff
    return out


def fmt(value: Decimal | None) -> str:
    if value is None:
        return "—"
    v = q2(value)
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.2f}"
