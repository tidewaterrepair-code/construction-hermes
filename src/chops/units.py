"""US customary unit handling with explicit conversions (Decimal only)."""

from __future__ import annotations

from decimal import Decimal

from .money import D

# Canonical units by dimension; factors convert *to* the canonical unit.
_LENGTH = {"in": Decimal("1") / 12, "ft": Decimal("1"), "lf": Decimal("1"), "yd": Decimal("3"), "m": Decimal("3.280839895")}
_AREA = {"sqin": Decimal("1") / 144, "sqft": Decimal("1"), "sf": Decimal("1"), "sqyd": Decimal("9"), "sy": Decimal("9"),
         "sq": Decimal("100"),  # roofing square
         "sqm": Decimal("10.7639104167")}
_VOLUME = {"cuin": Decimal("1") / 1728, "cuft": Decimal("1"), "cf": Decimal("1"), "cuyd": Decimal("27"), "cy": Decimal("27"),
           "gal": Decimal("0.133680556")}
_DIMENSIONS = {"length": (_LENGTH, "ft"), "area": (_AREA, "sqft"), "volume": (_VOLUME, "cuft")}

COUNT_UNITS = {"ea", "each", "pc", "box", "bag", "roll", "sheet", "pack", "bundle", "hr", "day", "ls", "lot", "trip", "load", "bf", "mbf", "ton", "lb"}


class UnitError(ValueError):
    pass


def normalize_unit(unit: str) -> str:
    u = unit.strip().lower().replace(".", "").replace(" ", "")
    aliases = {"feet": "ft", "foot": "ft", "inch": "in", "inches": "in", "linealft": "lf", "linft": "lf",
               "sqfeet": "sqft", "squarefeet": "sqft", "ft2": "sqft", "yd3": "cuyd", "cubicyard": "cuyd",
               "cubicyards": "cuyd", "hours": "hr", "hrs": "hr", "hour": "hr", "each": "ea", "lumpsum": "ls"}
    return aliases.get(u, u)


def dimension_of(unit: str) -> str | None:
    u = normalize_unit(unit)
    for name, (table, _) in _DIMENSIONS.items():
        if u in table:
            return name
    return None


def convert(value: object, from_unit: str, to_unit: str) -> Decimal:
    v = D(value)
    fu, tu = normalize_unit(from_unit), normalize_unit(to_unit)
    if fu == tu:
        return v
    if fu in ("bf",) and tu == "mbf":
        return v / 1000
    if fu == "mbf" and tu == "bf":
        return v * 1000
    fd, td = dimension_of(fu), dimension_of(tu)
    if fd is None or fd != td:
        raise UnitError(f"cannot convert {from_unit} to {to_unit}")
    table = _DIMENSIONS[fd][0]
    return v * table[fu] / table[tu]


def board_feet(thickness_in: object, width_in: object, length_ft: object, count: object = 1) -> Decimal:
    """Nominal board feet = T(in) x W(in) x L(ft) / 12 per piece."""
    return D(thickness_in) * D(width_in) * D(length_ft) / 12 * D(count)


def packs_needed(quantity: object, pack_size: object) -> int:
    """Whole packs required to cover ``quantity`` (ceil)."""
    q, p = D(quantity), D(pack_size)
    if p <= 0:
        raise UnitError("pack size must be positive")
    n = (q / p).to_integral_value(rounding="ROUND_CEILING")
    return int(n)


def unit_price_per_base(pack_price: object, pack_size: object) -> Decimal:
    p = D(pack_size)
    if p <= 0:
        raise UnitError("pack size must be positive")
    return D(pack_price) / p
