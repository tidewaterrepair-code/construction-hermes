import datetime as dt
from decimal import Decimal as D

import pytest

from chops import money, units
from chops.formula import FormulaError, evaluate
from chops.pricing import Line, Policy, compute


def test_markup_vs_margin_reference_values():
    assert money.price_for_margin(D("10000"), D("0.30")) == D("14285.71")
    assert money.price_with_markup(D("10000"), D("0.30")) == D("13000.00")


def test_margin_bounds_and_zero_revenue():
    with pytest.raises(money.MoneyError):
        money.price_for_margin(D("100"), D("1"))
    with pytest.raises(money.MoneyError):
        money.price_for_margin(D("100"), D("-0.1"))
    assert money.gross_margin(D("0"), D("50")) is None
    assert money.gross_margin(D("14285.71"), D("10000")) == D("0.3000")


def test_floats_rejected_and_rounding_half_up():
    with pytest.raises(money.MoneyError):
        money.D(0.1)
    assert money.q2(D("2.345")) == D("2.35")
    assert money.q2(D("2.344")) == D("2.34")


def test_allocate_sums_exactly():
    parts = money.allocate(D("100.00"), [D("1"), D("1"), D("1")])
    assert sum(parts) == D("100.00")
    assert sorted(parts) == [D("33.33"), D("33.33"), D("33.34")]


def test_unit_conversions():
    assert units.convert("18", "in", "ft") == D("1.5")
    assert units.convert("27", "cuft", "cuyd") == D("1")
    assert units.convert("2", "sqyd", "sqft") == D("18")
    assert units.board_feet(2, 6, 12) == D("12")
    assert units.packs_needed("101", "50") == 3
    assert units.unit_price_per_base("45.00", "50") == D("0.9")
    with pytest.raises(units.UnitError):
        units.convert(1, "ft", "sqft")


def test_formula_is_safe():
    assert evaluate("ceil(length_ft*12/16)+1", {"length_ft": D("12")}) == D("10")
    for bad in ("__import__('os')", "a.b", "[1]", "lambda: 1", "open('x')"):
        with pytest.raises(FormulaError):
            evaluate(bad, {"a": D(1)})
    with pytest.raises(FormulaError):
        evaluate("width_ft*2", {})


def _lines():
    return [
        Line(1, "material", "Joists", D("10"), "ea", D("20.00"), waste_pct=D("0.10"), rate_status="verified"),
        Line(2, "labor", "Carpenter hours", D("40"), "hr", D("50.00"), rate_status="owner_entered"),
        Line(3, "subcontract", "Electrical (quote)", D("1"), "ls", D("1000.00"), rate_status="verified"),
    ]


def _policy(**kw):
    base = dict(pricing_mode="turnkey", quote_type="firm", contingency_pct=D("0.05"), overhead_pct=D("0.10"),
                profit_method="margin", profit_pct=D("0.30"), tax_mode="none", labor_burden_pct=D("0.20"),
                dimensions={"length": {"value": "12", "source": "field_measured"}})
    base.update(kw)
    return Policy(**base)


def test_turnkey_totals_and_no_double_counting():
    t = compute(_lines(), _policy(), today=dt.date(2026, 10, 1))
    # materials 10*1.1*20=220; labor 40*50*1.2=2400; sub 1000 -> 3620
    assert t["direct_cost"] == "3620.00"
    assert t["contingency"] == "181.00"          # 5% of 3620
    assert t["overhead"] == "380.10"             # 10% of 3801
    assert t["cost_basis"] == "4181.10"
    assert t["price_before_discount"] == str(money.price_for_margin(D("4181.10"), D("0.30")))
    assert t["total"] == t["net_price"]           # tax_mode none
    assert t["firm_quote_ready"] is True
    assert D(t["margin_after_overhead"]) == D("0.3000")


def test_labor_only_excludes_materials():
    t = compute(_lines(), _policy(pricing_mode="labor_only"), today=dt.date(2026, 10, 1))
    assert t["direct_cost"] == "3400.00"
    assert t["excluded_customer_supplied"] == ["Joists"]


def test_discount_reduces_price_not_cost_and_sales_tax_after_discount():
    lines = [Line(1, "material", "Boards", D("100"), "ea", D("10"), rate_status="verified", taxable=True),
             Line(2, "labor", "Labor", D("10"), "hr", D("100"), rate_status="verified")]
    t = compute(lines, _policy(profit_method="markup", profit_pct=D("0.20"), contingency_pct=D("0"), overhead_pct=D("0"),
                               labor_burden_pct=D("0"), discount_pct=D("0.10"), tax_mode="sales_tax_on_price",
                               sales_tax_pct=D("0.06")), today=dt.date(2026, 10, 1))
    assert t["cost_basis"] == "2000.00"
    assert t["price_before_discount"] == "2400.00"
    assert t["discount"] == "240.00"
    assert t["net_price"] == "2160.00"
    # taxable share = 2160 * 1000/2000 = 1080 -> 6% = 64.80
    assert t["sales_tax"] == "64.80"
    assert t["total"] == "2224.80"


def test_missing_rates_and_unverified_dimensions_block_firm_quote():
    lines = _lines() + [Line(4, "material", "Footings concrete", D("2"), "cuyd", None)]
    t = compute(lines, _policy(dimensions={"length": {"value": "12", "source": "photo_estimate"}}), today=dt.date(2026, 10, 1))
    assert t["firm_quote_ready"] is False
    codes = {i["code"] for i in t["issues"]}
    assert {"missing_rate", "dimension_not_verified"} <= codes
    # Missing line is visible, not silently priced
    assert t["lines"][3]["state"] == "missing" and t["lines"][3]["cost"] is None


def test_expired_and_provisional_rates_block_firm():
    lines = [Line(1, "material", "Boards", D("1"), "ea", D("5"), rate_status="verified", rate_valid_until=dt.date(2026, 1, 1)),
             Line(2, "labor", "Labor", D("1"), "hr", D("5"), rate_status="provisional")]
    t = compute(lines, _policy(), today=dt.date(2026, 10, 1))
    codes = [i["code"] for i in t["issues"]]
    assert "expired_rate" in codes and "provisional_rate" in codes
    assert not t["firm_quote_ready"]


def test_unset_policy_is_visible():
    t = compute(_lines(), _policy(profit_pct=None, overhead_pct=None, tax_mode="unconfigured"), today=dt.date(2026, 10, 1))
    assert t["total"] is None
    codes = {i["code"] for i in t["issues"]}
    assert {"profit_unset", "overhead_unset", "tax_unconfigured"} <= codes
