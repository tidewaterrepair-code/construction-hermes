"""Synthetic demonstration data. Refuses to run against a production database.

Everything here is invented for testing: names, prices, terms. Records are flagged
``is_synthetic`` and the database is marked environment=demo so it can never go LIVE.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy.orm import Session

from .authz import Actor
from .errors import Blocked
from .services import rates, settings

SYN = "SYNTHETIC DEMO - not real pricing"


def configure_demo_settings(session: Session, owner: Actor) -> None:
    if settings.get(session, "environment") not in (None, "demo"):
        raise Blocked("refusing to load synthetic settings into a non-demo database")
    settings._write(session, owner, "environment", "demo")
    settings._write(session, owner, "company_name", "Synthetic Demo Construction (NOT REAL)")
    settings._write(session, owner, "company_contact", "demo@example.invalid · (000) 000-0000")
    settings._write(session, owner, "commercial_terms", {
        "payment_milestones": [
            {"key": "deposit", "label": "Deposit on acceptance", "pct": "0.30"},
            {"key": "progress", "label": "Framing complete", "pct": "0.40"},
            {"key": "final", "label": "Substantial completion", "pct": "0.30"},
        ],
        "validity_days": 30,
        "terms_text": "SYNTHETIC DEMO TERMS - not owner-approved commercial terms.",
    })
    settings._write(session, owner, "estimating_defaults", {
        "overhead_pct": "0.10", "profit_method": "margin", "profit_pct": "0.30", "contingency_pct": "0.05",
        "labor_burden_pct": "0.20", "tax_mode": "none",
    })
    settings._write(session, owner, "invoice_terms_days", 15)
    settings._write(session, owner, "retainage_default_pct", "0.05")
    settings._write(session, owner, "mode", "SHADOW")


def load_demo_rates(session: Session, owner: Actor) -> list[dict[str, Any]]:
    today = dt.date.today()
    rows = [
        ("LUMBER-JOIST-LF", "Joist lumber (per LF) - demo", "material", "lf", "2.10"),
        ("DECKING-LF", "Decking board (per LF) - demo", "material", "lf", "3.25"),
        ("CONCRETE-BAG", "Concrete mix bag - demo", "material", "bag", "6.50"),
        ("HARDWARE-DECK-SQFT", "Deck hardware allowance per sqft - demo", "material", "sqft", "1.40"),
        ("LABOR-CARPENTER-HR", "Carpenter labor per hour - demo", "labor", "hr", "45.00"),
        ("DISPOSAL-LOAD", "Debris disposal load - demo", "disposal", "load", "275.00"),
        ("LUMBER-STUD-EA", "Stud - demo", "material", "ea", "4.75"),
        ("LUMBER-PLATE-LF", "Plate stock per LF - demo", "material", "lf", "0.95"),
    ]
    out = []
    for code, desc, cat, unit, cost in rows:
        out.append(rates.create(session, owner, code=code, description=desc, category=cat, unit=unit, unit_cost=cost,
                                source=SYN, source_date=today, status="owner_entered", synthetic=True))
    return out
