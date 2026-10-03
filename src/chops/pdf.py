"""Customer-facing PDFs (proposal, invoice). Never includes internal cost or profit data."""

from __future__ import annotations

import io
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from xml.sax.saxutils import escape

BRAND = colors.HexColor("#1f3a5f")
ACCENT = colors.HexColor("#c8102e")


def _styles():
    ss = getSampleStyleSheet()
    ss.add(ParagraphStyle("H1x", parent=ss["Heading1"], textColor=BRAND, fontSize=18, spaceAfter=4))
    ss.add(ParagraphStyle("H2x", parent=ss["Heading2"], textColor=BRAND, fontSize=12, spaceBefore=10, spaceAfter=4))
    ss.add(ParagraphStyle("Small", parent=ss["BodyText"], fontSize=8.5, leading=11, textColor=colors.HexColor("#444444")))
    ss.add(ParagraphStyle("Body", parent=ss["BodyText"], fontSize=10, leading=13))
    ss.add(ParagraphStyle("Warn", parent=ss["BodyText"], fontSize=10, leading=13, textColor=ACCENT))
    return ss


def _p(text: Any, style) -> Paragraph:
    return Paragraph(escape(str(text if text is not None else "")).replace("\n", "<br/>"), style)


def _bullets(items: list[Any], ss) -> list:
    out = []
    for it in items or []:
        label = it.get("condition") + (f" — {it['resolution']}" if it.get("resolution") else " — unresolved") \
            if isinstance(it, dict) else str(it)
        out.append(_p("• " + label, ss["Body"]))
    return out


def _header(c: dict[str, Any], ss, title: str) -> list:
    co = c["company"]
    rows = [[_p(co["name"], ss["H1x"]), _p(title, ss["H1x"])],
            [_p(co.get("contact") or "", ss["Small"]), _p(c.get("number_line", ""), ss["Small"])]]
    if co.get("license"):
        rows.append([_p(co["license"], ss["Small"]), ""])
    t = Table(rows, colWidths=[3.9 * inch, 3.1 * inch])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, -1), (-1, -1), 1.5, BRAND),
                           ("BOTTOMPADDING", (0, -1), (-1, -1), 8)]))
    return [t, Spacer(1, 8)]


def _money_table(rows: list[list[str]], ss, widths) -> Table:
    data = [[_p(c, ss["Body"]) for c in r] for r in rows]
    t = Table(data, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef5")), ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ("ALIGN", (-1, 0), (-1, -1), "RIGHT"), ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return t


def render_proposal(c: dict[str, Any]) -> bytes:
    buf = io.BytesIO()
    ss = _styles()
    doc = SimpleDocTemplate(buf, pagesize=LETTER, leftMargin=0.75 * inch, rightMargin=0.75 * inch,
                            topMargin=0.6 * inch, bottomMargin=0.6 * inch,
                            title=f"Proposal {c['proposal_ref']}", author=c["company"]["name"])
    story: list = _header({**c, "number_line": f"{c['proposal_ref']} · {c['estimate_ref']} rev {c['revision']}\nDate: {c['date']}\nValid until: {c['valid_until'] or 'see terms'}"}, ss, "Proposal")
    if c.get("synthetic"):
        story.append(_p("SYNTHETIC DEMONSTRATION DOCUMENT — NOT A REAL OFFER", ss["Warn"]))
    cust = c["customer"]
    story.append(_p(f"Prepared for: {cust['name']}\n{cust.get('site') or ''}", ss["Body"]))
    story.append(_p(c["title"], ss["H2x"]))
    if c.get("quote_type") == "rough_range":
        rr = c.get("range") or {}
        story.append(_p(f"PRELIMINARY ESTIMATE RANGE — NOT A FIRM PRICE: {rr.get('low')} to {rr.get('high')}. "
                        "A firm proposal requires field verification of the items listed under assumptions.", ss["Warn"]))
    if c.get("scope_summary"):
        story.append(_p(c["scope_summary"], ss["Body"]))
    story.append(_p("Scope and pricing", ss["H2x"]))
    rows = [["Item", "Amount"]] + [[ln["text"], ln["amount"]] for ln in c["lines"]]
    rows.append(["Subtotal", c["subtotal"]])
    if c.get("discount") and c["discount"] != "$0.00":
        rows.append(["Discount", "-" + c["discount"]])
    if c.get("sales_tax") and c["sales_tax"] != "$0.00":
        rows.append(["Sales tax", c["sales_tax"]])
    rows.append(["Total", c["total"]])
    story.append(_money_table(rows, ss, [5.4 * inch, 1.6 * inch]))
    for label, key in (("Included", "inclusions"), ("Not included", "exclusions"), ("Allowances", "allowances"),
                       ("Assumptions and provisional items", "assumptions"), ("Site conditions", "conditions")):
        if c.get(key):
            story.append(_p(label, ss["H2x"]))
            story += _bullets(c[key], ss)
    story.append(_p("Payment schedule", ss["H2x"]))
    if c.get("milestones"):
        ms = [["Milestone", "Amount"]] + [[f"{m['label']} ({m['pct_display']})", m["amount"]] for m in c["milestones"]]
        story.append(_money_table(ms, ss, [5.4 * inch, 1.6 * inch]))
    else:
        story.append(_p("[Payment terms not configured]", ss["Warn"]))
    if c.get("terms_text"):
        story.append(_p("Terms", ss["H2x"]))
        story.append(_p(c["terms_text"], ss["Small"]))
    if c.get("warranty_text"):
        story.append(_p(c["warranty_text"], ss["Small"]))
    accept = [
        [_p("Acceptance", ss["H2x"]), ""],
        [_p("Customer signature: ______________________________", ss["Body"]), _p("Date: ____________", ss["Body"])],
        [_p("Printed name: ______________________________", ss["Body"]), ""],
        [_p(f"{c['company']['name']}: ______________________________", ss["Body"]), _p("Date: ____________", ss["Body"])],
    ]
    story.append(Spacer(1, 10))
    story.append(KeepTogether(Table(accept, colWidths=[4.6 * inch, 2.4 * inch])))
    story.append(Spacer(1, 6))
    story.append(_p(f"Document fingerprint: {c['content_hash'][:16]}", ss["Small"]))
    doc.build(story)
    return buf.getvalue()


def render_invoice(c: dict[str, Any]) -> bytes:
    buf = io.BytesIO()
    ss = _styles()
    doc = SimpleDocTemplate(buf, pagesize=LETTER, leftMargin=0.75 * inch, rightMargin=0.75 * inch,
                            topMargin=0.6 * inch, bottomMargin=0.6 * inch, title=f"Invoice {c['number']}")
    story: list = _header({**c, "number_line": f"Invoice {c['number']}\nDate: {c['date']}\nDue: {c['due']}"}, ss, "Invoice")
    if c.get("synthetic"):
        story.append(_p("SYNTHETIC DEMONSTRATION DOCUMENT — NOT A REAL INVOICE", ss["Warn"]))
    story.append(_p(f"Bill to: {c['customer']['name']}\nProject: {c['job_name']} ({c['job_ref']})", ss["Body"]))
    rows = [["Description", "Amount"]] + [[ln["description"], ln["amount"]] for ln in c["lines"]]
    rows.append(["Subtotal", c["subtotal"]])
    if c.get("retainage_held") and c["retainage_held"] != "$0.00":
        rows.append([f"Retainage withheld ({c['retainage_pct']})", "-" + c["retainage_held"]])
    rows.append(["Amount due", c["total_due"]])
    story.append(_money_table(rows, ss, [5.4 * inch, 1.6 * inch]))
    doc.build(story)
    return buf.getvalue()
