"""Owner dashboard (server-rendered, phone-first).

Security: server-side sessions (hashed id in DB), HttpOnly SameSite=Strict cookie, CSRF token
on every POST, login rate limits, strict CSP (no inline script), no caching of private pages.
All writes go through the same service layer as the MCP tools.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from starlette.datastructures import FormData

from . import health, refs, timeutil
from .authz import Actor
from .config import get_settings
from .db import session_scope
from .errors import ChopsError, Forbidden
from .models import AuditEvent, Notification, Routine, User
from .money import D, fmt
from .services import (approvals, billing, costing, digest, documents, estimates, field, integrations, jobs, leads,
                       outbox, permits, procurement, proposals, rates, schedule, settings, users)

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
templates.env.filters["money"] = lambda v: fmt(None if v in (None, "") else D(v))
templates.env.filters["local"] = lambda v: timeutil.fmt_local(dt.datetime.fromisoformat(v) if isinstance(v, str) else v) if v else "—"
templates.env.filters["pct"] = lambda v: "—" if v in (None, "") else f"{(D(v) * 100).quantize(D('0.1'))}%"
templates.env.filters["tojson_pretty"] = lambda v: json.dumps(v, indent=1, default=str)
templates.env.filters["qty"] = lambda v: "—" if v in (None, "") else format(D(v).normalize(), "f")
templates.env.filters["refid"] = lambda r: (r or "-0").split("-")[-1]

app = FastAPI(title="Construction Hermes", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
COOKIE = "chops_session"

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; object-src 'none'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["Content-Security-Policy"] = CSP
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Permissions-Policy"] = "camera=(self), microphone=(self), geolocation=()"
    if not request.url.path.startswith("/static"):
        resp.headers["Cache-Control"] = "no-store"
    if get_settings().cookie_secure:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000"
    return resp


class NeedLogin(Exception):
    pass


@app.exception_handler(NeedLogin)
async def _need_login(request: Request, exc: NeedLogin):
    return RedirectResponse(f"/login?next={quote(request.url.path)}", status_code=303)


def _auth(request: Request, s) -> tuple[Actor, Any]:
    res = users.resolve_web_session(s, request.cookies.get(COOKIE))
    if res is None:
        raise NeedLogin()
    return res


async def _form(request: Request, ws) -> FormData:
    form = await request.form()
    if form.get("csrf") != ws.csrf_token:
        raise Forbidden("invalid form token; reload the page")
    return form


def _render(request: Request, s, actor: Actor, ws, name: str, status: int = 200, **ctx: Any) -> HTMLResponse:
    base_ctx = {
        "request": request, "actor": actor, "csrf": ws.csrf_token if ws else "", "mode": settings.mode(s),
        "kill": settings.kill_switch(s), "company": settings.company_display(s), "env": settings.environment(s),
        "flash": request.query_params.get("msg"), "error": request.query_params.get("err"),
        "pending_count": len(approvals.list_pending(s, actor)) if actor and actor.can("read:all") else 0,
        "ref": refs.ref,
    }
    return templates.TemplateResponse(request, name, {**base_ctx, **ctx}, status_code=status)


def _back(path: str, msg: str | None = None, err: str | None = None) -> RedirectResponse:
    q = []
    if msg:
        q.append("msg=" + quote(msg[:300]))
    if err:
        q.append("err=" + quote(err[:300]))
    sep = "&" if "?" in path else "?"
    return RedirectResponse(path + (sep + "&".join(q) if q else ""), status_code=303)


def _safe_next(n: str | None) -> str:
    return n if n and n.startswith("/") and not n.startswith("//") else "/"


def _opt(form: FormData, key: str) -> str | None:
    v = form.get(key)
    return v.strip() if isinstance(v, str) and v.strip() else None


async def _post(request: Request, back: str, fn) -> Response:
    """Run a form action in one transaction; business errors come back as a message."""
    try:
        with session_scope() as s:
            actor, ws = _auth(request, s)
            form = await _form(request, ws)
            result = fn(s, actor, form)
        if isinstance(result, Response):
            return result
        return _back(back if not isinstance(result, str) else result.split("|", 1)[0],
                     msg=result.split("|", 1)[1] if isinstance(result, str) and "|" in result else "Saved")
    except NeedLogin:
        raise
    except ChopsError as exc:
        detail = exc.detail.get("blockers") or exc.detail.get("allowed") or ""
        return _back(back, err=f"{exc.message} {detail if detail else ''}".strip())


# ====================================================================== auth


@app.get("/healthz")
def healthz() -> JSONResponse:
    res = health.check()
    return JSONResponse({"ok": res["ok"]}, status_code=200 if res["ok"] else 503)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    with session_scope() as s:
        return _render(request, s, None, None, "login.html", next=_safe_next(request.query_params.get("next")))


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    username = str(form.get("username", ""))[:80]
    ip = request.client.host if request.client else "unknown"
    limit = get_settings().rate_limit_login_per_min
    with session_scope() as s:
        if users.rate_limited(s, f"login-ip:{ip}", limit) or users.rate_limited(s, f"login-user:{username.lower()}", limit):
            return _back("/login", err="Too many attempts. Wait a minute.")
    with session_scope() as s:
        u = users.authenticate(s, username, str(form.get("password", "")))
        if u is None or u.role == "agent":
            return _back("/login", err="Wrong username or password")
        raw, _ = users.create_web_session(s, u, request.headers.get("user-agent"))
    resp = RedirectResponse(_safe_next(str(form.get("next") or "/")), status_code=303)
    resp.set_cookie(COOKIE, raw, httponly=True, secure=get_settings().cookie_secure, samesite="strict",
                    max_age=get_settings().session_hours * 3600, path="/")
    return resp


@app.post("/logout")
async def logout(request: Request):
    with session_scope() as s:
        actor, ws = _auth(request, s)
        await _form(request, ws)
        users.end_web_session(s, request.cookies.get(COOKIE))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE, path="/")
    return resp


# ====================================================================== today / approvals


@app.get("/", response_class=HTMLResponse)
def today(request: Request):
    with session_scope() as s:
        actor, ws = _auth(request, s)
        if not actor.can("read:financial"):
            return RedirectResponse("/jobs", status_code=303)
        d = digest.today(s, actor, max_items=12)
        notes = list(s.scalars(select(Notification).where(Notification.read_at.is_(None)).order_by(Notification.id.desc()).limit(5)))
        return _render(request, s, actor, ws, "today.html", d=d, notes=notes)


@app.get("/approvals", response_class=HTMLResponse)
def approvals_page(request: Request):
    with session_scope() as s:
        actor, ws = _auth(request, s)
        from .models import Approval

        recent = [approvals.approval_view(a) for a in s.scalars(select(Approval).where(Approval.status != "pending")
                                                                .order_by(Approval.id.desc()).limit(15))]
        return _render(request, s, actor, ws, "approvals.html", pending=approvals.list_pending(s, actor), recent=recent)


@app.get("/approvals/{aid}", response_class=HTMLResponse)
def approval_page(request: Request, aid: int):
    with session_scope() as s:
        actor, ws = _auth(request, s)
        a = approvals.approval_view(approvals.get(s, actor, aid))
        return _render(request, s, actor, ws, "approval.html", a=a)


@app.post("/approvals/{aid}/decide")
async def approval_decide(request: Request, aid: int):
    def fn(s, actor, form):
        res = approvals.decide(s, actor, aid, str(form.get("decision")), presented_hash=str(form.get("payload_hash")),
                               note=_opt(form, "note"))
        st = res["approval"]["status"]
        return f"/approvals/{aid}|{refs.ref('approval', aid)} {st}"
    return await _post(request, f"/approvals/{aid}", fn)


# ====================================================================== leads


@app.get("/leads", response_class=HTMLResponse)
def leads_page(request: Request, overdue: int = 0, closed: int = 0):
    with session_scope() as s:
        actor, ws = _auth(request, s)
        rows = leads.list_leads(s, actor, overdue_only=bool(overdue), include_closed=bool(closed))
        return _render(request, s, actor, ws, "leads.html", rows=rows, overdue=overdue, closed=closed)


@app.get("/leads/new", response_class=HTMLResponse)
def lead_new_page(request: Request):
    with session_scope() as s:
        actor, ws = _auth(request, s)
        return _render(request, s, actor, ws, "lead_new.html")


@app.post("/leads/new")
async def lead_new(request: Request):
    def fn(s, actor, form):
        res = leads.capture(s, actor, name=str(form.get("name", "")), phone=_opt(form, "phone"), email=_opt(form, "email"),
                            address=_opt(form, "address"), city=_opt(form, "city"), state=_opt(form, "state"),
                            job_type=_opt(form, "job_type"), scope=_opt(form, "scope"), timing=_opt(form, "timing"),
                            budget=_opt(form, "budget"), source=_opt(form, "source") or "manual")
        lid = res["lead"]["id"]
        note = "; ".join(res.get("review_reasons") or []) or "Lead saved"
        return f"/leads/{lid}|{note}"
    return await _post(request, "/leads/new", fn)


@app.get("/leads/{lid}", response_class=HTMLResponse)
def lead_page(request: Request, lid: int):
    with session_scope() as s:
        actor, ws = _auth(request, s)
        lead = leads.get(s, actor, lid)
        from .models import Appointment, Estimate

        ests = [estimates.view(s, actor, e.id) for e in s.scalars(select(Estimate).where(Estimate.lead_id == lid))]
        appts = [leads.appointment_view(a) for a in s.scalars(select(Appointment).where(Appointment.lead_id == lid))]
        return _render(request, s, actor, ws, "lead.html", lead=leads.lead_view(lead), history=leads.history(s, actor, lid),
                       followup=leads.followup_questions(s, actor, lid), estimates=ests, appts=appts,
                       allowed=sorted(leads.TRANSITIONS.get(lead.status, set())),
                       assemblies=rates.list_assemblies(s, actor))


@app.post("/leads/{lid}/action")
async def lead_action(request: Request, lid: int):
    def fn(s, actor, form):
        act = form.get("action")
        if act == "move":
            leads.transition(s, actor, lid, str(form.get("to_status")), _opt(form, "reason"))
        elif act == "reviewed":
            leads.update(s, actor, lid, mark_reviewed=True)
        elif act == "visit":
            leads.schedule_site_visit(s, actor, lid, str(form.get("starts_at")), int(form.get("duration") or 60))
        elif act == "confirm":
            leads.confirm_appointment(s, actor, int(str(form.get("appointment"))), str(form.get("evidence", "")))
        elif act == "estimate":
            e = estimates.create(s, actor, title=str(form.get("title") or "Estimate"),
                                 pricing_mode=str(form.get("pricing_mode")), lead_id=lid)
            return f"/estimates/{e['id']}|Estimate {e['ref']} started"
        elif act == "update":
            leads.update(s, actor, lid, **{k: _opt(form, k) for k in ("job_type", "scope", "timing", "budget",
                                                                       "next_action", "next_action_due")})
        return f"/leads/{lid}|Saved"
    return await _post(request, f"/leads/{lid}", fn)


# ====================================================================== estimates / proposals


@app.get("/estimates/{eid}", response_class=HTMLResponse)
def estimate_page(request: Request, eid: int, rev: int | None = None):
    with session_scope() as s:
        actor, ws = _auth(request, s)
        e = estimates.view(s, actor, eid, rev)
        from .models import Proposal

        props = [proposals.proposal_view(p) for p in s.scalars(select(Proposal).where(Proposal.estimate_id == eid)
                                                               .order_by(Proposal.id.desc()))]
        return _render(request, s, actor, ws, "estimate.html", e=e, props=props,
                       assemblies=rates.list_assemblies(s, actor), rate_list=rates.list_rates(s, actor))


@app.post("/estimates/{eid}/action")
async def estimate_action(request: Request, eid: int):
    def fn(s, actor, form):
        act = form.get("action")
        if act == "line":
            estimates.add_line(s, actor, eid, kind=str(form.get("kind")), description=str(form.get("description", "")),
                               quantity=_opt(form, "quantity"), unit=_opt(form, "unit"), rate_code=_opt(form, "rate_code"),
                               unit_cost=_opt(form, "unit_cost"), cost_source=_opt(form, "cost_source"),
                               waste_pct=_opt(form, "waste_pct") or "0", cost_code=_opt(form, "cost_code"))
        elif act == "remove_line":
            estimates.update_line(s, actor, eid, int(str(form.get("line"))), remove=True)
        elif act == "set_cost":
            estimates.update_line(s, actor, eid, int(str(form.get("line"))), unit_cost=_opt(form, "unit_cost"),
                                  cost_source=_opt(form, "cost_source"), quantity=_opt(form, "quantity"))
        elif act == "assembly":
            params = {}
            for k, v in form.items():
                if k.startswith("p_") and str(v).strip():
                    params[k[2:]] = {"value": str(v).strip(), "source": str(form.get("src_" + k[2:], "assumed"))}
            estimates.apply_assembly(s, actor, eid, str(form.get("assembly")), params)
        elif act == "policy":
            vals = {k: _opt(form, k) for k in estimates.POLICY_FIELDS}
            estimates.set_policy(s, actor, eid, **{k: v for k, v in vals.items() if v is not None})
        elif act == "text":
            rc = _opt(form, "resolve_condition")
            estimates.set_text_lists(s, actor, eid, scope_summary=_opt(form, "scope_summary"),
                                     add_inclusion=_opt(form, "add_inclusion"), add_exclusion=_opt(form, "add_exclusion"),
                                     add_assumption=_opt(form, "add_assumption"), add_condition=_opt(form, "add_condition"),
                                     resolve_condition=int(rc) if rc is not None else None, resolution=_opt(form, "resolution"))
        elif act == "dimension":
            estimates.set_dimension(s, actor, eid, str(form.get("name")), _opt(form, "value"), str(form.get("unit", "")),
                                    str(form.get("source")))
        elif act == "revision":
            estimates.new_revision(s, actor, eid, str(form.get("reason") or "revision"))
        elif act == "proposal":
            p = proposals.create_from_estimate(s, actor, eid, str(form.get("presentation") or "summary"))["proposal"]
            return f"/proposals/{p['id']}|Proposal {p['ref']} built"
        return f"/estimates/{eid}|Saved"
    return await _post(request, f"/estimates/{eid}", fn)


@app.get("/proposals/{pid}", response_class=HTMLResponse)
def proposal_page(request: Request, pid: int):
    with session_scope() as s:
        actor, ws = _auth(request, s)
        p = proposals.proposal_view(proposals.get(s, actor, pid), include_content=True)
        return _render(request, s, actor, ws, "proposal.html", p=p)


@app.post("/proposals/{pid}/action")
async def proposal_action(request: Request, pid: int):
    def fn(s, actor, form):
        act = form.get("action")
        if act == "request_issue":
            res = proposals.request_issue(s, actor, pid)
            return f"/approvals/{res['approval']['id']}|Approval requested"
        if act == "issued":
            proposals.mark_issued(s, actor, pid, str(form.get("evidence", "")))
        elif act == "accept":
            res = proposals.record_acceptance(s, actor, pid, str(form.get("evidence", "")),
                                              accept_after_expiry=form.get("after_expiry") == "1")
            return f"/jobs/{res['job']['id']}|Accepted. Job {res['job']['ref']} created"
        return f"/proposals/{pid}|Saved"
    return await _post(request, f"/proposals/{pid}", fn)


# ====================================================================== jobs


@app.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request):
    with session_scope() as s:
        actor, ws = _auth(request, s)
        return _render(request, s, actor, ws, "jobs.html", rows=jobs.list_jobs(s, actor))


@app.get("/jobs/{jid}", response_class=HTMLResponse)
def job_page(request: Request, jid: int):
    with session_scope() as s:
        actor, ws = _auth(request, s)
        j = jobs.get(s, actor, jid)
        ctx: dict[str, Any] = {"j": jobs.job_view(s, actor, j), "sched": schedule.job_schedule(s, actor, jid),
                               "logs": field.list_daily_logs(s, actor, jid), "docs": documents.list_documents(s, actor, job_id=jid),
                               "comp": permits.job_compliance(s, actor, jid), "cos": field.list_change_orders(s, actor, jid)}
        if actor.sees_financials:
            from .models import Invoice, Proposal

            ctx["fin"] = billing.job_financials(s, actor, jid)
            ctx["costs"] = costing.job_cost_report(s, actor, jid)
            ctx["invoices"] = [billing.invoice_view(i) for i in s.scalars(select(Invoice).where(Invoice.job_id == jid))]
            p = s.get(Proposal, j.source_proposal_id) if j.source_proposal_id else None
            ctx["milestones"] = (p.content.get("milestones") if p else []) or []
            ctx["crew"] = schedule.list_crew(s, actor)
        return _render(request, s, actor, ws, "job.html", **ctx)


@app.post("/jobs/{jid}/action")
async def job_action(request: Request, jid: int):
    def fn(s, actor, form):
        act = form.get("action")
        if act == "cost":
            res = costing.log_cost(s, actor, jid, kind=str(form.get("kind")), amount=str(form.get("amount")),
                                   description=str(form.get("description", "")), occurred_on=_opt(form, "occurred_on"),
                                   cost_code=_opt(form, "cost_code"), vendor_name=_opt(form, "vendor"))
            return f"/jobs/{jid}|Cost {res['cost']['ref']} logged ({res['cost']['status']})"
        if act == "log":
            field.draft_daily_log(s, actor, jid, original_note=str(form.get("note", "")),
                                  work_completed=_opt(form, "work_completed"), delays=_opt(form, "delays"),
                                  issues=_opt(form, "issues"), tomorrow_plan=_opt(form, "tomorrow_plan"))
        elif act == "co":
            field.create_change_order(s, actor, jid, title=str(form.get("title", "")), scope=str(form.get("scope", "")),
                                      price=str(form.get("price")), estimated_cost=_opt(form, "estimated_cost"),
                                      schedule_impact_days=int(form.get("days") or 0))
        elif act == "co_issue":
            res = field.request_change_order_issue(s, actor, int(str(form.get("co"))))
            return f"/approvals/{res['approval']['id']}|Approval requested"
        elif act == "co_customer":
            field.record_customer_approval(s, actor, int(str(form.get("co"))), str(form.get("evidence", "")))
        elif act == "invoice":
            res = billing.draft_milestone_invoice(s, actor, jid, str(form.get("milestone")))
            return f"/jobs/{jid}|Invoice {res['invoice']['number']} drafted"
        elif act == "invoice_issue":
            res = billing.request_issue(s, actor, int(str(form.get("invoice"))))
            return f"/approvals/{res['approval']['id']}|Approval requested"
        elif act == "payment":
            res = billing.record_payment(s, actor, job_id=jid, invoice_id=int(str(form.get("invoice"))),
                                         amount=str(form.get("amount")), received_on=str(form.get("received_on")),
                                         method=_opt(form, "method"), verification_source=_opt(form, "source"))
            return f"/jobs/{jid}|Payment {res['payment']['status']}"
        elif act == "task":
            crew = _opt(form, "crew")
            schedule.add_task(s, actor, jid, name=str(form.get("name", "")), starts_at=_opt(form, "starts_at"),
                              ends_at=_opt(form, "ends_at"), estimated_hours=_opt(form, "hours"),
                              weather_sensitive=form.get("weather") == "1", crew_ids=[int(crew)] if crew else [])
        elif act == "task_status":
            schedule.update_task_status(s, actor, int(str(form.get("task"))), str(form.get("status")))
        elif act == "status":
            jobs.transition(s, actor, jid, str(form.get("to_status")), _opt(form, "reason"))
        elif act == "permit":
            permits.add_permit(s, actor, jid, permit_type=str(form.get("permit_type", "")), jurisdiction=_opt(form, "jurisdiction"))
        elif act == "permit_update":
            permits.update_permit(s, actor, int(str(form.get("permit"))), status=_opt(form, "status"),
                                  status_source=_opt(form, "source"))
        elif act == "punch":
            permits.add_punch(s, actor, jid, str(form.get("description", "")))
        return f"/jobs/{jid}|Saved"
    return await _post(request, f"/jobs/{jid}", fn)


# ====================================================================== money


@app.get("/money", response_class=HTMLResponse)
def money_page(request: Request):
    with session_scope() as s:
        actor, ws = _auth(request, s)
        from .models import Invoice, Payment

        reported = [billing.payment_view(p) for p in s.scalars(select(Payment).where(Payment.status == "reported_unverified"))]
        drafts = [billing.invoice_view(i) for i in s.scalars(select(Invoice).where(
            Invoice.status.in_(("draft", "pending_approval", "approved"))).order_by(Invoice.id.desc()).limit(20))]
        return _render(request, s, actor, ws, "money.html", recv=billing.receivables(s, actor), reported=reported,
                       drafts=drafts, exceptions=costing.exceptions(s, actor), compliance=procurement.compliance(s, actor))


@app.post("/payments/{pid}/verify")
async def payment_verify(request: Request, pid: int):
    return await _post(request, "/money", lambda s, a, f: (billing.verify_payment(s, a, pid, str(f.get("source", ""))),
                                                           "/money|Payment verified")[1])


# ====================================================================== documents / upload


@app.get("/upload", response_class=HTMLResponse)
def upload_page(request: Request, job: int | None = None):
    with session_scope() as s:
        actor, ws = _auth(request, s)
        return _render(request, s, actor, ws, "upload.html", jobs_list=jobs.list_jobs(s, actor), job=job,
                       kinds=documents.KINDS)


@app.post("/upload")
async def upload(request: Request):
    form = await request.form()
    up = form.get("file")
    try:
        with session_scope() as s:
            actor, ws = _auth(request, s)
            if form.get("csrf") != ws.csrf_token:
                raise Forbidden("invalid form token")
            if up is None or not hasattr(up, "read"):
                raise ChopsError("choose a file")
            data = await up.read(get_settings().max_upload_bytes + 1)
            job_id = int(str(form.get("job"))) if form.get("job") else None
            d = documents.store(s, actor, data, filename=up.filename or "upload", kind=str(form.get("kind") or "photo"),
                                title=_opt(form, "title"), job_id=job_id)
        note = f"Stored {d['ref']}" + (f" (flags: {', '.join(d['flags'])})" if d["flags"] else "") + \
            (" - voice notes are stored; transcription is not connected" if d["kind"] == "voice_note" else "")
        return _back(f"/jobs/{job_id}" if job_id else "/upload", msg=note)
    except ChopsError as exc:
        return _back("/upload", err=exc.message)


@app.get("/documents/{did}/download")
def document_download(request: Request, did: int):
    with session_scope() as s:
        actor, _ = _auth(request, s)
        d, data = documents.read_bytes(s, actor, did)
        if d.status == "quarantined" and not actor.can("admin:settings"):
            raise Forbidden("quarantined document")
        name = d.original_filename.replace('"', "")
        inline = d.mime_type in ("application/pdf", "image/jpeg", "image/png", "image/webp") and d.status == "stored"
        return Response(content=data, media_type=d.mime_type if inline else "application/octet-stream",
                        headers={"Content-Disposition": f'{"inline" if inline else "attachment"}; filename="{name}"',
                                 "Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self'; style-src 'unsafe-inline'"})


# ====================================================================== system / settings


@app.get("/system", response_class=HTMLResponse)
def system_page(request: Request):
    with session_scope() as s:
        actor, ws = _auth(request, s)
        from .worker import ensure_routines

        ensure_routines(s)
        audit_rows = list(s.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(25)))
        return _render(request, s, actor, ws, "system.html", h=health.check(), integ=integrations.status_list(s),
                       obx=outbox.list_jobs(s, actor, limit=20), routines=list(s.scalars(select(Routine))),
                       audit=audit_rows, users_list=list(s.scalars(select(User).order_by(User.id))))


@app.post("/system/action")
async def system_action(request: Request):
    def fn(s, actor, form):
        act = form.get("action")
        if act == "kill_on":
            settings.engage_kill_switch(s, actor, str(form.get("reason") or "engaged from dashboard"))
        elif act == "kill_off":
            settings.release_kill_switch(s, actor, str(form.get("reason") or "released from dashboard"))
        elif act == "mode":
            settings.set_mode(s, actor, str(form.get("mode")), confirm_live=form.get("confirm_live") == "LIVE")
        elif act == "routine":
            from .services.audit import record

            if not actor.can("admin:settings"):
                raise Forbidden("owner only")
            r = s.get(Routine, str(form.get("name")))
            r.enabled = form.get("enabled") == "1"
            ch = _opt(form, "channel")
            if ch in ("dashboard", "telegram"):
                r.channel = ch
            record(s, actor, "routine.update", None, None, name=r.name, enabled=r.enabled, channel=r.channel)
        elif act == "outbox":
            outbox.resolve_unknown(s, actor, int(str(form.get("job"))), str(form.get("outcome")), str(form.get("evidence", "")))
        elif act == "notes_read":
            for n in s.scalars(select(Notification).where(Notification.read_at.is_(None))):
                n.read_at = timeutil.now()
        return "/system|Saved"
    return await _post(request, "/system", fn)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    with session_scope() as s:
        actor, ws = _auth(request, s)
        cur = settings.get_all(s)
        return _render(request, s, actor, ws, "settings.html", known=settings.KNOWN_KEYS, cur=cur,
                       channel_approvals=cur.get("channel_approvals", False))


@app.post("/settings")
async def settings_save(request: Request):
    def fn(s, actor, form):
        key = str(form.get("key"))
        if key == "channel_approvals":
            if not actor.can("admin:settings"):
                raise Forbidden("owner only")
            settings._write(s, actor, "channel_approvals", form.get("value") == "1")
            return "/settings|Chat approvals " + ("on" if form.get("value") == "1" else "off")
        raw = str(form.get("value", "")).strip()
        try:
            value = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            value = raw  # plain text values (e.g. company name)
        settings.put(s, actor, key, value)
        return f"/settings|{key} saved"
    return await _post(request, "/settings", fn)


@app.get("/export")
def export_all(request: Request):
    from .services.export import export_zip

    with session_scope() as s:
        actor, _ = _auth(request, s)
        data = export_zip(s, actor)
    return Response(content=data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="construction-hermes-export-{timeutil.today_local()}.zip"'})


@app.exception_handler(ChopsError)
async def _chops_error(request: Request, exc: ChopsError):
    with session_scope() as s:
        res = users.resolve_web_session(s, request.cookies.get(COOKIE))
        actor, ws = res if res else (None, None)
        return _render(request, s, actor, ws, "error.html", status=exc.http_status if exc.http_status >= 400 else 400,
                       message=exc.message)
