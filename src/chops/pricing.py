"""Estimate pricing engine (pure functions, no database).

Cost basis (in order, each counted exactly once):
  1. line direct costs
     - material/equipment/delivery/disposal/subcontract/other: qty x (1 + waste) x unit cost
     - labor: hours x rate x (1 + labor burden)
     - allowance: qty x unit cost (customer-visible allowance)
     - labor-only estimates exclude material lines (listed as customer/owner-supplied)
  2. material tax paid by the contractor (tax_mode = contractor_pays_material_tax) -> a cost
  3. contingency = (1 + 2) x contingency_pct
  4. overhead    = (1 + 2 + 3) x overhead_pct
  cost_basis = 1 + 2 + 3 + 4
Price:
  markup: price = cost_basis x (1 + profit_pct)
  margin: price = cost_basis / (1 - profit_pct)
Discount reduces price only (pct first, then fixed amount), never cost. Sales tax
(tax_mode = sales_tax_on_price) applies after discount to the taxable share of price.
Reported margins:
  gross_margin            = (net price - (direct + material tax + contingency)) / net price
  margin_after_overhead   = (net price - cost_basis) / net price
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .money import ONE, ZERO, D, allocate, gross_margin, price_for_margin, price_with_markup, q2

TAX_MODES = ("unconfigured", "none", "contractor_pays_material_tax", "sales_tax_on_price")
FIRM_DIMENSION_SOURCES = {"field_measured", "plans"}
ALL_DIMENSION_SOURCES = {"field_measured", "plans", "customer_supplied", "photo_estimate", "assumed"}


@dataclass
class Line:
    line_no: int
    kind: str
    description: str
    quantity: Decimal | None
    unit: str | None
    unit_cost: Decimal | None
    waste_pct: Decimal = ZERO
    rate_status: str | None = None
    rate_valid_until: dt.date | None = None
    taxable: bool = False
    cost_code: str | None = None


@dataclass
class Policy:
    pricing_mode: str  # labor_only | turnkey
    quote_type: str = "rough_range"
    contingency_pct: Decimal = ZERO
    overhead_pct: Decimal | None = None
    profit_method: str = "margin"
    profit_pct: Decimal | None = None
    discount_pct: Decimal = ZERO
    discount_amount: Decimal = ZERO
    tax_mode: str = "unconfigured"
    material_tax_pct: Decimal | None = None
    sales_tax_pct: Decimal | None = None
    labor_burden_pct: Decimal | None = None
    dimensions: dict[str, Any] = field(default_factory=dict)
    unresolved_conditions: list[Any] = field(default_factory=list)
    range_low_pct: Decimal = Decimal("0.10")
    range_high_pct: Decimal = Decimal("0.25")
    range_band_configured: bool = False


@dataclass
class Issue:
    code: str
    message: str
    blocks_firm: bool
    line_no: int | None = None

    def as_dict(self) -> dict[str, Any]:
        d = {"code": self.code, "message": self.message, "blocks_firm_quote": self.blocks_firm}
        if self.line_no is not None:
            d["line"] = self.line_no
        return d


def _line_cost(ln: Line, policy: Policy, issues: list[Issue], today: dt.date) -> tuple[Decimal | None, str]:
    if policy.pricing_mode == "labor_only" and ln.kind == "material":
        return None, "excluded_customer_supplied"
    if ln.quantity is None:
        issues.append(Issue("missing_quantity", f"line {ln.line_no} '{ln.description}': quantity missing", True, ln.line_no))
        return None, "missing"
    if ln.unit_cost is None:
        issues.append(Issue("missing_rate", f"line {ln.line_no} '{ln.description}': no rate", True, ln.line_no))
        return None, "missing"
    status = ln.rate_status or "unknown"
    if ln.rate_valid_until is not None and ln.rate_valid_until < today:
        status = "expired"
    if status == "expired":
        issues.append(Issue("expired_rate", f"line {ln.line_no} '{ln.description}': rate expired", True, ln.line_no))
    elif status in ("provisional", "unknown"):
        issues.append(Issue("provisional_rate", f"line {ln.line_no} '{ln.description}': rate is {status}", True, ln.line_no))
    qty = D(ln.quantity)
    cost = D(ln.unit_cost)
    if ln.kind == "labor":
        burden = policy.labor_burden_pct if policy.labor_burden_pct is not None else ZERO
        return qty * cost * (ONE + D(burden)), "priced"
    if ln.kind == "allowance":
        return qty * cost, "priced"
    return qty * (ONE + D(ln.waste_pct or 0)) * cost, "priced"


def compute(lines: list[Line], policy: Policy, today: dt.date | None = None) -> dict[str, Any]:
    today = today or dt.date.today()
    issues: list[Issue] = []
    rows: list[dict[str, Any]] = []
    by_kind: dict[str, Decimal] = {}
    by_code: dict[str, Decimal] = {}
    direct = ZERO
    material_cost = ZERO
    taxable_cost = ZERO
    excluded: list[str] = []

    if not lines:
        issues.append(Issue("no_lines", "estimate has no line items", True))

    for ln in lines:
        c, state = _line_cost(ln, policy, issues, today)
        row = {"line": ln.line_no, "kind": ln.kind, "description": ln.description, "state": state,
               "quantity": None if ln.quantity is None else str(ln.quantity), "unit": ln.unit,
               "unit_cost": None if ln.unit_cost is None else str(ln.unit_cost),
               "cost": None if c is None else str(q2(c)), "cost_code": ln.cost_code}
        rows.append(row)
        if state == "excluded_customer_supplied":
            excluded.append(ln.description)
            continue
        if c is None:
            continue
        c = q2(c)
        direct += c
        by_kind[ln.kind] = by_kind.get(ln.kind, ZERO) + c
        code = ln.cost_code or ln.kind
        by_code[code] = by_code.get(code, ZERO) + c
        if ln.kind == "material":
            material_cost += c
        if ln.taxable:
            taxable_cost += c

    if policy.labor_burden_pct is None and any(ln.kind == "labor" for ln in lines):
        issues.append(Issue("labor_burden_unset", "labor burden not set; labor rates treated as fully burdened", False))

    # Tax
    material_tax = ZERO
    if policy.tax_mode not in TAX_MODES:
        issues.append(Issue("tax_mode_invalid", f"unknown tax mode {policy.tax_mode}", True))
    elif policy.tax_mode == "unconfigured":
        issues.append(Issue("tax_unconfigured", "tax treatment not configured; totals exclude tax", True))
    elif policy.tax_mode == "contractor_pays_material_tax":
        if policy.material_tax_pct is None:
            issues.append(Issue("material_tax_rate_missing", "material tax rate not set", True))
        else:
            material_tax = q2(material_cost * D(policy.material_tax_pct))
    elif policy.tax_mode == "sales_tax_on_price" and policy.sales_tax_pct is None:
        issues.append(Issue("sales_tax_rate_missing", "sales tax rate not set", True))

    contingency = q2((direct + material_tax) * D(policy.contingency_pct or 0))
    if policy.overhead_pct is None:
        issues.append(Issue("overhead_unset", "overhead policy not set; overhead excluded from cost basis", True))
        overhead = ZERO
    else:
        overhead = q2((direct + material_tax + contingency) * D(policy.overhead_pct))
    job_cost = direct + material_tax + contingency
    cost_basis = job_cost + overhead

    price = discount = net_price = sales_tax = total = None
    gm = mao = None
    if policy.profit_pct is None:
        issues.append(Issue("profit_unset", "profit policy not set; no price computed", True))
    else:
        if policy.profit_method == "markup":
            price = price_with_markup(cost_basis, D(policy.profit_pct))
        elif policy.profit_method == "margin":
            price = price_for_margin(cost_basis, D(policy.profit_pct))
        else:
            raise ValueError("profit_method must be markup or margin")
        discount = q2(price * D(policy.discount_pct or 0)) + q2(D(policy.discount_amount or 0))
        if discount > price:
            issues.append(Issue("discount_exceeds_price", "discount exceeds price; capped at price", True))
            discount = price
        net_price = price - discount
        sales_tax = ZERO
        if policy.tax_mode == "sales_tax_on_price" and policy.sales_tax_pct is not None and direct > 0:
            taxable_share = net_price * taxable_cost / direct
            sales_tax = q2(taxable_share * D(policy.sales_tax_pct))
        total = net_price + sales_tax
        gm = gross_margin(net_price, job_cost)
        mao = gross_margin(net_price, cost_basis)

    for name, dim in (policy.dimensions or {}).items():
        src = (dim or {}).get("source")
        if src not in ALL_DIMENSION_SOURCES:
            issues.append(Issue("dimension_source_unknown", f"dimension '{name}' has no recorded source", True))
        elif src not in FIRM_DIMENSION_SOURCES:
            issues.append(Issue("dimension_not_verified", f"dimension '{name}' is {src}; field-verify before a firm quote", True))
        if (dim or {}).get("value") in (None, ""):
            issues.append(Issue("dimension_missing", f"dimension '{name}' has no value", True))

    open_conditions = [c for c in (policy.unresolved_conditions or []) if not (isinstance(c, dict) and c.get("resolution"))]
    if open_conditions:
        issues.append(Issue("unresolved_conditions", f"{len(open_conditions)} unresolved site condition(s)", True))

    firm_blockers = [i for i in issues if i.blocks_firm]
    rng = None
    if total is not None:
        rng = {"low": str(q2(total * (ONE - D(policy.range_low_pct)))),
               "high": str(q2(total * (ONE + D(policy.range_high_pct)))),
               "band": {"low_pct": str(policy.range_low_pct), "high_pct": str(policy.range_high_pct),
                        "source": "owner setting" if policy.range_band_configured else "system default assumption"}}

    def s(v: Decimal | None) -> str | None:
        return None if v is None else str(q2(v))

    return {
        "pricing_mode": policy.pricing_mode,
        "quote_type": policy.quote_type,
        "lines": rows,
        "excluded_customer_supplied": excluded,
        "direct_cost": s(direct),
        "direct_by_kind": {k: s(v) for k, v in sorted(by_kind.items())},
        "direct_by_cost_code": {k: s(v) for k, v in sorted(by_code.items())},
        "material_tax": s(material_tax),
        "contingency": s(contingency),
        "overhead": s(overhead),
        "job_cost": s(job_cost),
        "cost_basis": s(cost_basis),
        "profit_method": policy.profit_method,
        "profit_pct": None if policy.profit_pct is None else str(policy.profit_pct),
        "price_before_discount": s(price),
        "discount": s(discount),
        "net_price": s(net_price),
        "sales_tax": s(sales_tax),
        "total": s(total),
        "gross_margin": None if gm is None else str(gm),
        "margin_after_overhead": None if mao is None else str(mao),
        "rough_range": rng,
        "issues": [i.as_dict() for i in issues],
        "firm_quote_ready": not firm_blockers and total is not None,
        "firm_blockers": [i.message for i in firm_blockers],
    }


def customer_line_prices(rows: list[dict[str, Any]], net_price: Decimal) -> list[Decimal]:
    """Allocate the net price over priced lines in proportion to cost (sums exactly)."""
    weights = [D(r["cost"]) if r.get("cost") is not None and r["state"] == "priced" else ZERO for r in rows]
    return allocate(net_price, weights)
