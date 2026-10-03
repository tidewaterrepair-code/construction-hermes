"""Relational schema. Money is NUMERIC (Decimal in Python); never float.

Conventions
- ``created_at``/``updated_at`` are UTC ``timestamptz``; display converts to America/New_York.
- Tables with ``version`` use optimistic concurrency (SQLAlchemy ``version_id_col``).
- Status columns are constrained with CHECK constraints mirroring ``chops.states``.
- ``is_synthetic`` marks demonstration/test records; production screens filter them out.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

MONEY = Numeric(14, 2)
RATE = Numeric(16, 4)
QTY = Numeric(16, 4)
PCT = Numeric(7, 4)  # fraction, e.g. 0.3000 = 30%


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB, list[Any]: JSONB}


class Stamped:
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)


# --------------------------------------------------------------------------- org / identity

class OrgSetting(Base):
    """Key/value business configuration. Unknowns are absent keys, never guesses."""

    __tablename__ = "org_settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    updated_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)


ROLES = ("owner", "office", "foreman", "crew", "agent", "viewer")


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True)
    display_name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(20))
    password_hash: Mapped[str | None] = mapped_column(String(255))
    telegram_user_id: Mapped[str | None] = mapped_column(String(40), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (CheckConstraint(_in("role", ROLES), name="ck_users_role"),)


class ApiToken(Base):
    """Bearer tokens for service identities (Hermes agent). Only a SHA-256 hash is stored."""

    __tablename__ = "api_tokens"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(80))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    user: Mapped[User] = relationship()


class WebSession(Base):
    __tablename__ = "web_sessions"
    id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    csrf_token: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    user_agent: Mapped[str | None] = mapped_column(String(300))
    user: Mapped[User] = relationship()


class RateLimitHit(Base):
    __tablename__ = "rate_limit_hits"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bucket: Mapped[str] = mapped_column(String(200), index=True)
    at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


# --------------------------------------------------------------------------- CRM

CONTACT_KINDS = ("customer", "vendor", "subcontractor", "inspector", "other")


class Contact(Stamped, Base):
    __tablename__ = "contacts"
    kind: Mapped[str] = mapped_column(String(20), default="customer")
    name: Mapped[str] = mapped_column(String(200))
    company: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(40))
    phone_normalized: Mapped[str | None] = mapped_column(String(20), index=True)
    email: Mapped[str | None] = mapped_column(String(254))
    email_normalized: Mapped[str | None] = mapped_column(String(254), index=True)
    notes: Mapped[str | None] = mapped_column(Text)
    trade: Mapped[str | None] = mapped_column(String(80))
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    merged_into_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"))
    __table_args__ = (CheckConstraint(_in("kind", CONTACT_KINDS), name="ck_contacts_kind"),)


class Site(Stamped, Base):
    __tablename__ = "sites"
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"))
    address_line: Mapped[str] = mapped_column(String(300))
    city: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str | None] = mapped_column(String(2))
    postal_code: Mapped[str | None] = mapped_column(String(10))
    address_normalized: Mapped[str] = mapped_column(String(300), index=True)
    # Jurisdiction stays "unconfirmed" until verified; never inferred silently.
    jurisdiction: Mapped[str | None] = mapped_column(String(120))
    jurisdiction_status: Mapped[str] = mapped_column(String(20), default="unconfirmed")
    access_notes: Mapped[str | None] = mapped_column(Text)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)


LEAD_STATUSES = ("inquiry", "qualified", "site_visit", "estimating", "proposal", "follow_up", "won", "lost")


class Lead(Stamped, Base):
    __tablename__ = "leads"
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"))
    site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id"))
    status: Mapped[str] = mapped_column(String(20), default="inquiry")
    job_type: Mapped[str | None] = mapped_column(String(80))
    scope_text: Mapped[str | None] = mapped_column(Text)
    timing_text: Mapped[str | None] = mapped_column(String(300))
    budget_text: Mapped[str | None] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(80), default="manual")
    source_ref: Mapped[str | None] = mapped_column(String(200))
    next_action: Mapped[str | None] = mapped_column(String(300))
    next_action_due: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    # Field-level extraction provenance: {field: {value, confidence, source}}; needs_review if uncertain.
    extraction: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    possible_duplicate_of_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"))
    lost_reason: Mapped[str | None] = mapped_column(String(300))
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    contact: Mapped[Contact] = relationship()
    site: Mapped[Site | None] = relationship()
    __mapper_args__ = {"version_id_col": version}
    __table_args__ = (CheckConstraint(_in("status", LEAD_STATUSES), name="ck_leads_status"),)


class LeadTransition(Base):
    __tablename__ = "lead_transitions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), index=True)
    from_status: Mapped[str | None] = mapped_column(String(20))
    to_status: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str | None] = mapped_column(String(500))
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


APPOINTMENT_STATUSES = ("tentative", "confirmed", "cancelled", "completed")


class Appointment(Stamped, Base):
    __tablename__ = "appointments"
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"))
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"))
    kind: Mapped[str] = mapped_column(String(40), default="site_visit")
    starts_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="tentative")
    confirmation_evidence: Mapped[str | None] = mapped_column(String(500))
    notes: Mapped[str | None] = mapped_column(Text)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (
        CheckConstraint(_in("status", APPOINTMENT_STATUSES), name="ck_appt_status"),
        CheckConstraint("status <> 'confirmed' OR confirmation_evidence IS NOT NULL", name="ck_appt_confirm_evidence"),
    )


class IntegrationEvent(Base):
    """Every inbound provider event, deduplicated on (provider, external_id)."""

    __tablename__ = "integration_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    provider: Mapped[str] = mapped_column(String(60))
    external_id: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(60))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    received_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    status: Mapped[str] = mapped_column(String(20), default="received")
    result_entity: Mapped[str | None] = mapped_column(String(60))
    result_id: Mapped[int | None] = mapped_column(BigInteger)
    error: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("provider", "external_id", name="uq_integration_event"),)


# --------------------------------------------------------------------------- estimating

RATE_STATUSES = ("verified", "owner_entered", "provisional", "expired")
RATE_CATEGORIES = ("material", "labor", "equipment", "subcontract", "delivery", "disposal", "other")


class Rate(Stamped, Base):
    __tablename__ = "rates"
    code: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(String(300))
    category: Mapped[str] = mapped_column(String(20))
    unit: Mapped[str] = mapped_column(String(20))
    unit_cost: Mapped[Decimal] = mapped_column(RATE)
    status: Mapped[str] = mapped_column(String(20), default="provisional")
    source: Mapped[str] = mapped_column(String(300))
    source_date: Mapped[dt.date] = mapped_column(Date)
    geography: Mapped[str | None] = mapped_column(String(120))
    valid_until: Mapped[dt.date | None] = mapped_column(Date)
    vendor_contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"))
    supersedes_id: Mapped[int | None] = mapped_column(ForeignKey("rates.id"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (
        CheckConstraint(_in("status", RATE_STATUSES), name="ck_rates_status"),
        CheckConstraint(_in("category", RATE_CATEGORIES), name="ck_rates_category"),
        CheckConstraint("unit_cost >= 0", name="ck_rates_nonneg"),
        Index("ix_rates_code_active", "code", "active"),
    )


class Assembly(Stamped, Base):
    """Editable quantity template. ``is_example`` templates are not local pricing or engineering."""

    __tablename__ = "assemblies"
    code: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    trade: Mapped[str] = mapped_column(String(80))
    description: Mapped[str | None] = mapped_column(Text)
    parameters: Mapped[list[Any]] = mapped_column(JSONB)  # [{name, unit, label, required, default}]
    components: Mapped[list[Any]] = mapped_column(JSONB)  # [{kind, description, rate_code, unit, qty, waste, cost_code}]
    notes: Mapped[str | None] = mapped_column(Text)
    is_example: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


ESTIMATE_STATUSES = ("draft", "proposed", "accepted", "declined", "superseded", "void")
PRICING_MODES = ("labor_only", "turnkey")


class Estimate(Stamped, Base):
    __tablename__ = "estimates"
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"))
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", use_alter=True, name="fk_estimates_job"))
    title: Mapped[str] = mapped_column(String(200))
    pricing_mode: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="draft")
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    revisions: Mapped[list["EstimateRevision"]] = relationship(
        back_populates="estimate", order_by="EstimateRevision.revision_no"
    )
    __table_args__ = (
        CheckConstraint(_in("status", ESTIMATE_STATUSES), name="ck_est_status"),
        CheckConstraint(_in("pricing_mode", PRICING_MODES), name="ck_est_mode"),
    )


REVISION_STATUSES = ("draft", "locked", "superseded")
QUOTE_TYPES = ("rough_range", "firm")
PROFIT_METHODS = ("markup", "margin")


class EstimateRevision(Base):
    __tablename__ = "estimate_revisions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    estimate_id: Mapped[int] = mapped_column(ForeignKey("estimates.id"), index=True)
    revision_no: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    quote_type: Mapped[str] = mapped_column(String(20), default="rough_range")
    # {name: {value, unit, source}} where source in field_measured|plans|customer_supplied|photo_estimate|assumed
    dimensions: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    contingency_pct: Mapped[Decimal] = mapped_column(PCT, default=Decimal("0"))
    overhead_pct: Mapped[Decimal | None] = mapped_column(PCT)
    profit_method: Mapped[str] = mapped_column(String(10), default="margin")
    profit_pct: Mapped[Decimal | None] = mapped_column(PCT)
    discount_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    discount_pct: Mapped[Decimal] = mapped_column(PCT, default=Decimal("0"))
    material_tax_pct: Mapped[Decimal | None] = mapped_column(PCT)  # tax the contractor pays on materials (a cost)
    sales_tax_pct: Mapped[Decimal | None] = mapped_column(PCT)  # tax charged to the customer, if applicable
    tax_mode: Mapped[str] = mapped_column(String(30), default="unconfigured")
    labor_burden_pct: Mapped[Decimal | None] = mapped_column(PCT)
    scope_summary: Mapped[str | None] = mapped_column(Text)
    inclusions: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    exclusions: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    assumptions: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    unresolved_conditions: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    totals: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    locked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1)
    estimate: Mapped[Estimate] = relationship(back_populates="revisions")
    items: Mapped[list["EstimateItem"]] = relationship(
        back_populates="revision", order_by="EstimateItem.line_no", cascade="all, delete-orphan"
    )
    __mapper_args__ = {"version_id_col": version}
    __table_args__ = (
        UniqueConstraint("estimate_id", "revision_no", name="uq_est_rev"),
        CheckConstraint(_in("status", REVISION_STATUSES), name="ck_rev_status"),
        CheckConstraint(_in("quote_type", QUOTE_TYPES), name="ck_rev_quote_type"),
        CheckConstraint(_in("profit_method", PROFIT_METHODS), name="ck_rev_profit_method"),
        CheckConstraint("profit_method <> 'margin' OR profit_pct IS NULL OR (profit_pct >= 0 AND profit_pct < 1)", name="ck_rev_margin_range"),
        CheckConstraint("discount_amount >= 0 AND discount_pct >= 0 AND discount_pct < 1", name="ck_rev_discount"),
    )


ITEM_KINDS = ("material", "labor", "subcontract", "equipment", "delivery", "disposal", "allowance", "other")


class EstimateItem(Base):
    __tablename__ = "estimate_items"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    revision_id: Mapped[int] = mapped_column(ForeignKey("estimate_revisions.id", ondelete="CASCADE"), index=True)
    line_no: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20))
    description: Mapped[str] = mapped_column(String(300))
    cost_code: Mapped[str | None] = mapped_column(String(40))
    quantity: Mapped[Decimal | None] = mapped_column(QTY)
    unit: Mapped[str | None] = mapped_column(String(20))
    quantity_source: Mapped[str | None] = mapped_column(String(200))
    waste_pct: Mapped[Decimal] = mapped_column(PCT, default=Decimal("0"))
    rate_id: Mapped[int | None] = mapped_column(ForeignKey("rates.id"))
    # Snapshot of the rate at the time of costing (rate records may later change/expire).
    unit_cost: Mapped[Decimal | None] = mapped_column(RATE)
    rate_status: Mapped[str | None] = mapped_column(String(20))
    rate_source: Mapped[str | None] = mapped_column(String(300))
    rate_source_date: Mapped[dt.date | None] = mapped_column(Date)
    taxable: Mapped[bool] = mapped_column(Boolean, default=False)
    customer_visible_text: Mapped[str | None] = mapped_column(String(300))
    notes: Mapped[str | None] = mapped_column(Text)
    revision: Mapped[EstimateRevision] = relationship(back_populates="items")
    __table_args__ = (
        CheckConstraint(_in("kind", ITEM_KINDS), name="ck_item_kind"),
        CheckConstraint("quantity IS NULL OR quantity >= 0", name="ck_item_qty"),
        CheckConstraint("waste_pct >= 0 AND waste_pct < 1", name="ck_item_waste"),
    )


PROPOSAL_STATUSES = ("draft", "pending_approval", "approved", "issued", "accepted", "declined", "superseded", "void")


class Proposal(Stamped, Base):
    """Immutable customer-facing snapshot of one estimate revision."""

    __tablename__ = "proposals"
    estimate_id: Mapped[int] = mapped_column(ForeignKey("estimates.id"), index=True)
    estimate_revision_id: Mapped[int] = mapped_column(ForeignKey("estimate_revisions.id"))
    proposal_no: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    content: Mapped[dict[str, Any]] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    total: Mapped[Decimal] = mapped_column(MONEY)
    pdf_document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"))
    approval_id: Mapped[int | None] = mapped_column(ForeignKey("approvals.id"))
    issued_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    acceptance_evidence: Mapped[str | None] = mapped_column(String(500))
    acceptance_document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"))
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    __mapper_args__ = {"version_id_col": version}
    __table_args__ = (
        UniqueConstraint("estimate_id", "proposal_no", name="uq_proposal_no"),
        CheckConstraint(_in("status", PROPOSAL_STATUSES), name="ck_proposal_status"),
        CheckConstraint("status <> 'accepted' OR acceptance_evidence IS NOT NULL", name="ck_proposal_accept_evidence"),
    )


# --------------------------------------------------------------------------- approvals

APPROVAL_STATUSES = ("pending", "approved", "rejected", "expired", "invalidated", "executing", "executed", "failed", "cancelled")


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    action_type: Mapped[str] = mapped_column(String(60))
    target_type: Mapped[str] = mapped_column(String(60))
    target_id: Mapped[int] = mapped_column(BigInteger)
    target_revision: Mapped[str] = mapped_column(String(80))
    summary: Mapped[str] = mapped_column(String(500))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    payload_hash: Mapped[str] = mapped_column(String(64))
    destination: Mapped[str | None] = mapped_column(String(300))
    amount: Mapped[Decimal | None] = mapped_column(MONEY)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    requested_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    requested_via: Mapped[str] = mapped_column(String(40))
    conversation_key: Mapped[str | None] = mapped_column(String(120), index=True)
    decided_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    decided_via: Mapped[str | None] = mapped_column(String(40))
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(String(500))
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    execution_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (
        CheckConstraint(_in("status", APPROVAL_STATUSES), name="ck_approval_status"),
        CheckConstraint(
            "status NOT IN ('approved','rejected','executing','executed','failed') OR decided_by_id IS NOT NULL",
            name="ck_approval_decider",
        ),
        Index("ix_approvals_status", "status"),
    )


# --------------------------------------------------------------------------- jobs

JOB_STATUSES = ("planned", "active", "on_hold", "complete", "closed", "cancelled")


class Job(Stamped, Base):
    __tablename__ = "jobs"
    name: Mapped[str] = mapped_column(String(200))
    customer_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"))
    site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id"))
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"))
    status: Mapped[str] = mapped_column(String(20), default="planned")
    source_proposal_id: Mapped[int | None] = mapped_column(ForeignKey("proposals.id", use_alter=True, name="fk_jobs_proposal"), unique=True)
    source_estimate_revision_id: Mapped[int | None] = mapped_column(ForeignKey("estimate_revisions.id", use_alter=True, name="fk_jobs_est_rev"))
    contract_value: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    retainage_pct: Mapped[Decimal] = mapped_column(PCT, default=Decimal("0"))
    planned_start: Mapped[dt.date | None] = mapped_column(Date)
    planned_finish: Mapped[dt.date | None] = mapped_column(Date)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    customer: Mapped[Contact] = relationship()
    site: Mapped[Site | None] = relationship()
    __mapper_args__ = {"version_id_col": version}
    __table_args__ = (CheckConstraint(_in("status", JOB_STATUSES), name="ck_job_status"),)


class JobAssignment(Base):
    """Grants a non-owner user access to a job. Foreman/crew see only assigned jobs."""

    __tablename__ = "job_assignments"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    __table_args__ = (UniqueConstraint("job_id", "user_id", name="uq_job_assignment"),)


class JobBudgetLine(Base):
    """Estimated cost by cost code, frozen from the accepted estimate revision."""

    __tablename__ = "job_budget_lines"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    cost_code: Mapped[str] = mapped_column(String(40))
    description: Mapped[str] = mapped_column(String(200))
    estimated_cost: Mapped[Decimal] = mapped_column(MONEY)
    source: Mapped[str] = mapped_column(String(60))  # estimate_revision:<id> | change_order:<id>
    forecast_to_complete: Mapped[Decimal | None] = mapped_column(MONEY)
    forecast_note: Mapped[str | None] = mapped_column(String(300))


class CrewMember(Stamped, Base):
    __tablename__ = "crew_members"
    name: Mapped[str] = mapped_column(String(120))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    trade: Mapped[str | None] = mapped_column(String(80))
    phone: Mapped[str | None] = mapped_column(String(40))
    daily_capacity_hours: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("8"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)


TASK_STATUSES = ("todo", "scheduled", "in_progress", "blocked", "done", "cancelled")


class Task(Stamped, Base):
    __tablename__ = "tasks"
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    phase: Mapped[str | None] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="todo")
    starts_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    estimated_hours: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    weather_sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    requires_permit_id: Mapped[int | None] = mapped_column(ForeignKey("permits.id"))
    requires_inspection_id: Mapped[int | None] = mapped_column(ForeignKey("inspections.id"))
    requires_po_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_orders.id"))
    responsible_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    notes: Mapped[str | None] = mapped_column(Text)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (
        CheckConstraint(_in("status", TASK_STATUSES), name="ck_task_status"),
        CheckConstraint("ends_at IS NULL OR starts_at IS NULL OR ends_at > starts_at", name="ck_task_times"),
    )


class TaskDependency(Base):
    __tablename__ = "task_dependencies"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    depends_on_task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    __table_args__ = (
        UniqueConstraint("task_id", "depends_on_task_id", name="uq_task_dep"),
        CheckConstraint("task_id <> depends_on_task_id", name="ck_task_dep_self"),
    )


class TaskAssignment(Base):
    __tablename__ = "task_assignments"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    crew_member_id: Mapped[int] = mapped_column(ForeignKey("crew_members.id"), index=True)
    __table_args__ = (UniqueConstraint("task_id", "crew_member_id", name="uq_task_assign"),)


class DailyLog(Stamped, Base):
    __tablename__ = "daily_logs"
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    log_date: Mapped[dt.date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    original_note: Mapped[str] = mapped_column(Text)
    extraction: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    work_completed: Mapped[str | None] = mapped_column(Text)
    labor: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    materials: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    delays: Mapped[str | None] = mapped_column(Text)
    issues: Mapped[str | None] = mapped_column(Text)
    tomorrow_plan: Mapped[str | None] = mapped_column(Text)
    photo_document_ids: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (CheckConstraint(_in("status", ("draft", "final")), name="ck_dlog_status"),)


CO_STATUSES = ("draft", "pending_approval", "approved_to_issue", "issued", "customer_approved", "rejected", "void")


class ChangeOrder(Stamped, Base):
    __tablename__ = "change_orders"
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    revision_no: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str] = mapped_column(String(200))
    scope: Mapped[str] = mapped_column(Text)
    price: Mapped[Decimal] = mapped_column(MONEY)
    estimated_cost: Mapped[Decimal | None] = mapped_column(MONEY)
    cost_code: Mapped[str | None] = mapped_column(String(40))
    schedule_impact_days: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    evidence_document_ids: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    content_hash: Mapped[str] = mapped_column(String(64))
    customer_approval_evidence: Mapped[str | None] = mapped_column(String(500))
    customer_approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    supersedes_id: Mapped[int | None] = mapped_column(ForeignKey("change_orders.id"))
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    __mapper_args__ = {"version_id_col": version}
    __table_args__ = (
        CheckConstraint(_in("status", CO_STATUSES), name="ck_co_status"),
        CheckConstraint("status <> 'customer_approved' OR customer_approval_evidence IS NOT NULL", name="ck_co_evidence"),
        UniqueConstraint("job_id", "number", "revision_no", name="uq_co_number_rev"),
    )


# --------------------------------------------------------------------------- procurement

class VendorDocument(Stamped, Base):
    __tablename__ = "vendor_documents"
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"), index=True)
    doc_type: Mapped[str] = mapped_column(String(40))  # insurance_coi | license | w9 | other
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"))
    identifier: Mapped[str | None] = mapped_column(String(120))
    expires_on: Mapped[dt.date | None] = mapped_column(Date)
    verification_status: Mapped[str] = mapped_column(String(30), default="unverified")
    verification_note: Mapped[str | None] = mapped_column(String(500))
    __table_args__ = (
        CheckConstraint(_in("verification_status", ("unverified", "owner_reviewed", "verified_with_issuer")), name="ck_vdoc_verif"),
    )


class VendorQuote(Stamped, Base):
    __tablename__ = "vendor_quotes"
    vendor_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"))
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"))
    reference: Mapped[str | None] = mapped_column(String(120))
    revision_no: Mapped[int] = mapped_column(Integer, default=1)
    received_on: Mapped[dt.date] = mapped_column(Date)
    expires_on: Mapped[dt.date | None] = mapped_column(Date)
    # [{description, qty, unit, pack_size, unit_price, item_key}]
    lines: Mapped[list[Any]] = mapped_column(JSONB)
    delivery_cost: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    tax_assumption: Mapped[str] = mapped_column(String(120), default="not stated")
    tax_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    availability: Mapped[str | None] = mapped_column(String(200))
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"))
    is_subcontract: Mapped[bool] = mapped_column(Boolean, default=False)
    scope: Mapped[str | None] = mapped_column(Text)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)


PO_STATUSES = ("draft", "pending_approval", "approved", "issued", "partially_received", "received", "cancelled")


class PurchaseOrder(Stamped, Base):
    __tablename__ = "purchase_orders"
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"))
    number: Mapped[str] = mapped_column(String(40), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    vendor_quote_id: Mapped[int | None] = mapped_column(ForeignKey("vendor_quotes.id"))
    needed_by: Mapped[dt.date | None] = mapped_column(Date)
    lead_time_days: Mapped[int | None] = mapped_column(Integer)
    subtotal: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    delivery_cost: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    tax_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    total: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    content_hash: Mapped[str | None] = mapped_column(String(64))
    notes: Mapped[str | None] = mapped_column(Text)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    lines: Mapped[list["POLine"]] = relationship(order_by="POLine.line_no", cascade="all, delete-orphan")
    __mapper_args__ = {"version_id_col": version}
    __table_args__ = (CheckConstraint(_in("status", PO_STATUSES), name="ck_po_status"),)


class POLine(Base):
    __tablename__ = "po_lines"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    po_id: Mapped[int] = mapped_column(ForeignKey("purchase_orders.id", ondelete="CASCADE"), index=True)
    line_no: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(String(300))
    quantity: Mapped[Decimal] = mapped_column(QTY)
    unit: Mapped[str] = mapped_column(String(20))
    unit_cost: Mapped[Decimal] = mapped_column(RATE)
    cost_code: Mapped[str] = mapped_column(String(40))
    amount: Mapped[Decimal] = mapped_column(MONEY)


# --------------------------------------------------------------------------- costs and billing

COST_KINDS = ("labor", "material", "subcontract", "equipment", "other")
COST_STATUSES = ("unreviewed", "approved", "needs_review", "reversed")


class CostEntry(Stamped, Base):
    """Actual cost. Corrections are new rows that reference (and reverse) the original."""

    __tablename__ = "cost_entries"
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    cost_code: Mapped[str | None] = mapped_column(String(40))
    description: Mapped[str] = mapped_column(String(300))
    amount: Mapped[Decimal] = mapped_column(MONEY)
    occurred_on: Mapped[dt.date] = mapped_column(Date)
    vendor_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"))
    vendor_name: Mapped[str | None] = mapped_column(String(200))
    po_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_orders.id"))
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"))
    source: Mapped[str] = mapped_column(String(40), default="manual")
    external_id: Mapped[str | None] = mapped_column(String(120))
    hours: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    status: Mapped[str] = mapped_column(String(20), default="unreviewed")
    match_note: Mapped[str | None] = mapped_column(String(300))
    corrects_id: Mapped[int | None] = mapped_column(ForeignKey("cost_entries.id"))
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (
        CheckConstraint(_in("kind", COST_KINDS), name="ck_cost_kind"),
        CheckConstraint(_in("status", COST_STATUSES), name="ck_cost_status"),
        UniqueConstraint("source", "external_id", name="uq_cost_external"),
    )


INVOICE_STATUSES = ("draft", "pending_approval", "approved", "issued", "partially_paid", "paid", "void")


class Invoice(Stamped, Base):
    __tablename__ = "invoices"
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    number: Mapped[str] = mapped_column(String(40), unique=True)
    kind: Mapped[str] = mapped_column(String(20), default="milestone")
    milestone_key: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(20), default="draft")
    lines: Mapped[list[Any]] = mapped_column(JSONB)
    subtotal: Mapped[Decimal] = mapped_column(MONEY)
    tax: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    retainage_pct: Mapped[Decimal] = mapped_column(PCT, default=Decimal("0"))
    retainage_held: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    total_due: Mapped[Decimal] = mapped_column(MONEY)
    issued_on: Mapped[dt.date | None] = mapped_column(Date)
    due_on: Mapped[dt.date | None] = mapped_column(Date)
    content_hash: Mapped[str] = mapped_column(String(64))
    external_system: Mapped[str | None] = mapped_column(String(40))
    external_id: Mapped[str | None] = mapped_column(String(120))
    change_order_id: Mapped[int | None] = mapped_column(ForeignKey("change_orders.id"))
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    __mapper_args__ = {"version_id_col": version}
    __table_args__ = (
        CheckConstraint(_in("status", INVOICE_STATUSES), name="ck_inv_status"),
        UniqueConstraint("external_system", "external_id", name="uq_inv_external"),
        UniqueConstraint("job_id", "milestone_key", name="uq_inv_milestone"),
    )


PAYMENT_STATUSES = ("reported_unverified", "verified", "reconciled", "reversed")


class Payment(Stamped, Base):
    __tablename__ = "payments"
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(MONEY)
    received_on: Mapped[dt.date] = mapped_column(Date)
    method: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(25), default="reported_unverified")
    verification_source: Mapped[str | None] = mapped_column(String(300))
    applies_to_retainage: Mapped[bool] = mapped_column(Boolean, default=False)
    external_system: Mapped[str | None] = mapped_column(String(40))
    external_id: Mapped[str | None] = mapped_column(String(120))
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (
        CheckConstraint(_in("status", PAYMENT_STATUSES), name="ck_pay_status"),
        CheckConstraint("amount > 0", name="ck_pay_positive"),
        CheckConstraint("status = 'reported_unverified' OR verification_source IS NOT NULL", name="ck_pay_verified_source"),
        UniqueConstraint("external_system", "external_id", name="uq_pay_external"),
    )


# --------------------------------------------------------------------------- permits & knowledge

PERMIT_STATUSES = ("not_determined", "not_required_verified", "required", "applied", "issued", "expired", "closed")


class Permit(Stamped, Base):
    __tablename__ = "permits"
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    jurisdiction: Mapped[str | None] = mapped_column(String(120))
    permit_type: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(30), default="not_determined")
    application_ref: Mapped[str | None] = mapped_column(String(120))
    submitted_on: Mapped[dt.date | None] = mapped_column(Date)
    issued_on: Mapped[dt.date | None] = mapped_column(Date)
    expires_on: Mapped[dt.date | None] = mapped_column(Date)
    status_source: Mapped[str | None] = mapped_column(String(500))
    notes: Mapped[str | None] = mapped_column(Text)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (
        CheckConstraint(_in("status", PERMIT_STATUSES), name="ck_permit_status"),
        CheckConstraint("status NOT IN ('issued','not_required_verified') OR status_source IS NOT NULL", name="ck_permit_source"),
    )


INSPECTION_RESULTS = ("not_scheduled", "scheduled", "unverified", "passed", "failed", "partial")


class Inspection(Stamped, Base):
    __tablename__ = "inspections"
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    permit_id: Mapped[int | None] = mapped_column(ForeignKey("permits.id"))
    inspection_type: Mapped[str] = mapped_column(String(80))
    scheduled_for: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[str] = mapped_column(String(20), default="not_scheduled")
    result_source: Mapped[str | None] = mapped_column(String(500))
    deficiencies: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (
        CheckConstraint(_in("result", INSPECTION_RESULTS), name="ck_insp_result"),
        CheckConstraint("result NOT IN ('passed','failed','partial') OR result_source IS NOT NULL", name="ck_insp_source"),
    )


class Rfi(Stamped, Base):
    __tablename__ = "rfis"
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    question: Mapped[str] = mapped_column(Text)
    directed_to: Mapped[str | None] = mapped_column(String(200))
    answer: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="open")
    due_on: Mapped[dt.date | None] = mapped_column(Date)
    __table_args__ = (UniqueConstraint("job_id", "number", name="uq_rfi_number"),)


class PunchItem(Stamped, Base):
    __tablename__ = "punch_items"
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    description: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20), default="open")
    evidence_document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"))
    __table_args__ = (CheckConstraint(_in("status", ("open", "done", "verified")), name="ck_punch_status"),)


class ResearchSource(Stamped, Base):
    """A cited official source (code, permit page). Not a legal determination."""

    __tablename__ = "research_sources"
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"))
    permit_id: Mapped[int | None] = mapped_column(ForeignKey("permits.id"))
    url: Mapped[str] = mapped_column(String(1000))
    title: Mapped[str] = mapped_column(String(300))
    publisher: Mapped[str | None] = mapped_column(String(200))
    retrieved_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    edition: Mapped[str | None] = mapped_column(String(120))
    effective_date: Mapped[dt.date | None] = mapped_column(Date)
    passage: Mapped[str | None] = mapped_column(Text)
    review_status: Mapped[str] = mapped_column(String(30), default="unreviewed")


DOC_STATUSES = ("stored", "quarantined", "rejected")


class Document(Stamped, Base):
    __tablename__ = "documents"
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", use_alter=True, name="fk_documents_job"), index=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))  # plan | spec | quote | photo | receipt | contract | sop | proposal_pdf | voice_note | other
    title: Mapped[str] = mapped_column(String(300))
    original_filename: Mapped[str] = mapped_column(String(300))
    mime_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    storage_key: Mapped[str] = mapped_column(String(200))
    revision_of_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"))
    revision_no: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="stored")
    rejection_reason: Mapped[str | None] = mapped_column(String(300))
    # [{page, text}] extracted as untrusted data.
    pages: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    text_content: Mapped[str] = mapped_column(Text, default="")
    search_vector = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', coalesce(title,'') || ' ' || coalesce(text_content,''))", persisted=True)
    )
    flags: Mapped[list[Any]] = mapped_column(JSONB, default=list)  # e.g. ["contains_instructions"]
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (
        CheckConstraint(_in("status", DOC_STATUSES), name="ck_doc_status"),
        Index("ix_documents_fts", "search_vector", postgresql_using="gin"),
    )


# --------------------------------------------------------------------------- durability

OUTBOX_STATUSES = ("pending", "leased", "succeeded", "failed", "dead", "unknown", "blocked", "simulated")


class OutboxJob(Base):
    __tablename__ = "outbox_jobs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(60))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    external_effect: Mapped[bool] = mapped_column(Boolean, default=False)
    approval_id: Mapped[int | None] = mapped_column(ForeignKey("approvals.id"))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    next_attempt_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    lease_owner: Mapped[str | None] = mapped_column(String(80))
    lease_expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    provider_ref: Mapped[str | None] = mapped_column(String(200))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (
        CheckConstraint(_in("status", OUTBOX_STATUSES), name="ck_outbox_status"),
        Index("ix_outbox_ready", "status", "next_attempt_at"),
    )


class AuditEvent(Base):
    """Append-only (enforced by trigger + grants for the app role; not against a DB superuser)."""

    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    actor_role: Mapped[str | None] = mapped_column(String(20))
    via: Mapped[str | None] = mapped_column(String(40))
    action: Mapped[str] = mapped_column(String(80))
    entity_type: Mapped[str | None] = mapped_column(String(60))
    entity_id: Mapped[int | None] = mapped_column(BigInteger)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    __table_args__ = (Index("ix_audit_entity", "entity_type", "entity_id"),)


INTEGRATION_STATUSES = ("connected", "disconnected", "degraded")


class Integration(Base):
    __tablename__ = "integrations"
    name: Mapped[str] = mapped_column(String(60), primary_key=True)
    kind: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), default="disconnected")
    detail: Mapped[str | None] = mapped_column(String(500))
    verification: Mapped[str] = mapped_column(String(30), default="none")  # none | contract_test | live_read | live_delivery
    last_success_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(500))
    __table_args__ = (CheckConstraint(_in("status", INTEGRATION_STATUSES), name="ck_integration_status"),)


class Routine(Base):
    __tablename__ = "routines"
    name: Mapped[str] = mapped_column(String(60), primary_key=True)
    description: Mapped[str] = mapped_column(String(300))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    local_time: Mapped[str] = mapped_column(String(5))  # HH:MM America/New_York
    weekdays: Mapped[str] = mapped_column(String(20), default="0,1,2,3,4")  # Mon=0
    channel: Mapped[str] = mapped_column(String(30), default="dashboard")
    uses_model: Mapped[bool] = mapped_column(Boolean, default=False)
    last_run_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_result: Mapped[str | None] = mapped_column(String(500))


class Notification(Base):
    """Owner-facing digest/alert records (dashboard inbox; optionally delivered via a channel)."""

    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    read_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class LearningProposal(Base):
    """Evidence-backed suggestions (productivity, omissions). Never applied without review."""

    __tablename__ = "learning_proposals"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(40))
    subject: Mapped[str] = mapped_column(String(200))
    proposal: Mapped[dict[str, Any]] = mapped_column(JSONB)
    evidence: Mapped[list[Any]] = mapped_column(JSONB)
    sample_size: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="proposed")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    decided_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    __table_args__ = (CheckConstraint(_in("status", ("proposed", "accepted", "rejected")), name="ck_learn_status"),)


class UsageRecord(Base):
    """Attributable model/service usage reported to this service (local cap; not provider billing)."""

    __tablename__ = "usage_records"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    source: Mapped[str] = mapped_column(String(60))
    model: Mapped[str | None] = mapped_column(String(80))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("0"))
    note: Mapped[str | None] = mapped_column(String(200))
