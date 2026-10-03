"""Permits, inspections, RFIs, punch lists, and cited official sources.

Nothing here determines code compliance. Permit status and inspection results stay
unverified unless backed by an authorized source or a clearly attributed owner entry.
"""

from __future__ import annotations

import datetime as dt
import html
import re
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import timeutil
from ..authz import Actor, require, require_job_access
from ..errors import Forbidden, NotFound, ValidationFailed
from ..models import PERMIT_STATUSES, Inspection, Job, Permit, PunchItem, ResearchSource, Rfi
from ..refs import ref
from . import audit, documents

# Official-source research is restricted to government and code-publisher domains.
OFFICIAL_SUFFIXES = (".gov", ".vbgov.com", "vbgov.com", "iccsafe.org", "codes.iccsafe.org", "law.lis.virginia.gov",
                     "dhcd.virginia.gov", "virginia.gov", "nfpa.org", "osha.gov")


def permit_view(p: Permit) -> dict[str, Any]:
    return {"ref": ref("permit", p.id), "id": p.id, "job": ref("job", p.job_id), "type": p.permit_type,
            "jurisdiction": p.jurisdiction, "status": p.status, "application_ref": p.application_ref,
            "submitted_on": timeutil.iso(p.submitted_on), "issued_on": timeutil.iso(p.issued_on),
            "expires_on": timeutil.iso(p.expires_on), "status_source": p.status_source, "notes": p.notes}


def add_permit(session: Session, actor: Actor, job_id: int, *, permit_type: str, jurisdiction: str | None = None,
               notes: str | None = None) -> dict[str, Any]:
    require(actor, "write:job")
    require_job_access(session, actor, job_id, write=True)
    job = session.get(Job, job_id)
    p = Permit(job_id=job_id, permit_type=permit_type, jurisdiction=jurisdiction, status="not_determined", notes=notes,
               is_synthetic=job.is_synthetic, created_by_id=actor.user_id)
    session.add(p)
    session.flush()
    audit.record(session, actor, "permit.add", "permit", p.id, job=job_id, type=permit_type)
    return permit_view(p)


def update_permit(session: Session, actor: Actor, permit_id: int, *, status: str | None = None,
                  status_source: str | None = None, application_ref: str | None = None, submitted_on: str | None = None,
                  issued_on: str | None = None, expires_on: str | None = None, notes: str | None = None) -> dict[str, Any]:
    require(actor, "write:job")
    p = session.get(Permit, permit_id)
    if p is None:
        raise NotFound("permit not found")
    require_job_access(session, actor, p.job_id, write=True)
    if status:
        if status not in PERMIT_STATUSES:
            raise ValidationFailed(f"status must be one of {PERMIT_STATUSES}")
        if status in ("issued", "not_required_verified"):
            if not status_source:
                raise ValidationFailed("issued / not-required needs a source (permit number, portal record, official letter)")
            if not actor.can("payment:verify"):
                raise Forbidden("only the owner/office can mark a permit issued or not required")
        if status == "applied" and not actor.can("payment:verify"):
            # Submitting a permit is an external commitment; the agent can only record it as reported.
            status_source = f"reported via {actor.role}: {status_source or 'no source'}"
        p.status = status
    if status_source:
        p.status_source = status_source[:500]
    for k, v in (("submitted_on", submitted_on), ("issued_on", issued_on), ("expires_on", expires_on)):
        if v:
            setattr(p, k, dt.date.fromisoformat(v))
    if application_ref:
        p.application_ref = application_ref
    if notes:
        p.notes = notes
    audit.record(session, actor, "permit.update", "permit", p.id, status=status, source=status_source)
    return permit_view(p)


def inspection_view(i: Inspection) -> dict[str, Any]:
    return {"ref": ref("inspection", i.id), "id": i.id, "job": ref("job", i.job_id), "permit": ref("permit", i.permit_id),
            "type": i.inspection_type, "scheduled_for": timeutil.iso(i.scheduled_for),
            "scheduled_local": timeutil.fmt_local(i.scheduled_for), "result": i.result, "result_source": i.result_source,
            "deficiencies": i.deficiencies}


def add_inspection(session: Session, actor: Actor, job_id: int, *, inspection_type: str, permit_id: int | None = None,
                   scheduled_for: str | None = None) -> dict[str, Any]:
    require(actor, "write:job")
    require_job_access(session, actor, job_id, write=True)
    job = session.get(Job, job_id)
    i = Inspection(job_id=job_id, permit_id=permit_id, inspection_type=inspection_type,
                   scheduled_for=timeutil.parse_local(scheduled_for) if scheduled_for else None,
                   result="scheduled" if scheduled_for else "not_scheduled", is_synthetic=job.is_synthetic,
                   created_by_id=actor.user_id)
    session.add(i)
    session.flush()
    audit.record(session, actor, "inspection.add", "inspection", i.id, job=job_id)
    return inspection_view(i)


def record_inspection_result(session: Session, actor: Actor, inspection_id: int, *, result: str, source: str,
                             deficiencies: list[str] | None = None) -> dict[str, Any]:
    i = session.get(Inspection, inspection_id)
    if i is None:
        raise NotFound("inspection not found")
    require_job_access(session, actor, i.job_id, write=True)
    if result not in ("passed", "failed", "partial"):
        raise ValidationFailed("result must be passed, failed, or partial")
    if not source or len(source.strip()) < 5:
        raise ValidationFailed("a source is required (inspector card photo, portal record, owner observation)")
    if actor.role not in ("owner", "office"):
        # Field/agent reports stay unverified until the owner confirms.
        i.result = "unverified"
        i.result_source = f"reported by {actor.role} ({actor.display_name}): claimed {result}; {source}"[:500]
    else:
        i.result = result
        i.result_source = f"{actor.display_name or actor.role}: {source}"[:500]
    i.deficiencies = [{"item": d, "status": "open"} for d in (deficiencies or [])]
    audit.record(session, actor, "inspection.result", "inspection", i.id, result=i.result, source=source)
    return inspection_view(i)


def add_rfi(session: Session, actor: Actor, job_id: int, question: str, directed_to: str | None = None,
            due_on: str | None = None) -> dict[str, Any]:
    require(actor, "write:job")
    require_job_access(session, actor, job_id, write=True)
    n = (session.scalar(select(func.max(Rfi.number)).where(Rfi.job_id == job_id)) or 0) + 1
    r = Rfi(job_id=job_id, number=n, question=question, directed_to=directed_to,
            due_on=dt.date.fromisoformat(due_on) if due_on else None, created_by_id=actor.user_id)
    session.add(r)
    session.flush()
    audit.record(session, actor, "rfi.add", "rfi", r.id, job=job_id)
    return {"ref": ref("rfi", r.id), "number": n, "question": question, "status": r.status, "due_on": due_on}


def answer_rfi(session: Session, actor: Actor, rfi_id: int, answer: str) -> dict[str, Any]:
    require(actor, "write:job")
    r = session.get(Rfi, rfi_id)
    if r is None:
        raise NotFound("RFI not found")
    r.answer = answer
    r.status = "answered"
    audit.record(session, actor, "rfi.answer", "rfi", r.id)
    return {"ref": ref("rfi", r.id), "status": r.status}


def add_punch(session: Session, actor: Actor, job_id: int, description: str) -> dict[str, Any]:
    if not (actor.can("write:job") or actor.can("write:field")):
        raise Forbidden("not allowed")
    require_job_access(session, actor, job_id, write=True)
    p = PunchItem(job_id=job_id, description=description, created_by_id=actor.user_id)
    session.add(p)
    session.flush()
    audit.record(session, actor, "punch.add", "punch", p.id, job=job_id)
    return {"ref": ref("punch", p.id), "description": description, "status": "open"}


def complete_punch(session: Session, actor: Actor, punch_id: int, evidence_document_id: int | None = None) -> dict[str, Any]:
    p = session.get(PunchItem, punch_id)
    if p is None:
        raise NotFound("punch item not found")
    require_job_access(session, actor, p.job_id, write=True)
    if actor.role in ("owner", "office") and evidence_document_id:
        p.status = "verified"
    else:
        p.status = "done"
    p.evidence_document_id = evidence_document_id
    audit.record(session, actor, "punch.complete", "punch", p.id, status=p.status)
    return {"ref": ref("punch", p.id), "status": p.status}


def job_compliance(session: Session, actor: Actor, job_id: int) -> dict[str, Any]:
    require_job_access(session, actor, job_id)
    permits = [permit_view(p) for p in session.scalars(select(Permit).where(Permit.job_id == job_id))]
    insps = [inspection_view(i) for i in session.scalars(select(Inspection).where(Inspection.job_id == job_id))]
    rfis = [{"ref": ref("rfi", r.id), "number": r.number, "question": r.question, "status": r.status,
             "due_on": timeutil.iso(r.due_on)} for r in session.scalars(select(Rfi).where(Rfi.job_id == job_id))]
    punch = [{"ref": ref("punch", p.id), "description": p.description, "status": p.status}
             for p in session.scalars(select(PunchItem).where(PunchItem.job_id == job_id))]
    sources = [{"ref": ref("research", s.id), "title": s.title, "url": s.url, "retrieved_at": timeutil.iso(s.retrieved_at),
                "edition": s.edition, "review_status": s.review_status}
               for s in session.scalars(select(ResearchSource).where(ResearchSource.job_id == job_id))]
    gaps = []
    if not permits:
        gaps.append("permit requirement not determined for this job")
    gaps += [f"{p['ref']} {p['type']}: {p['status']}" for p in permits if p["status"] in ("not_determined", "required")]
    gaps += [f"{i['ref']} {i['type']}: {i['result']}" for i in insps if i["result"] == "unverified"]
    return {"job": ref("job", job_id), "permits": permits, "inspections": insps, "rfis": rfis, "punch": punch,
            "sources": sources, "gaps": gaps,
            "note": "Code and permit questions need verified project-specific sources and qualified review."}


# ------------------------------------------------------------------ official-source research


def _is_official(host: str) -> bool:
    host = host.lower()
    return any(host == s.lstrip(".") or host.endswith(s if s.startswith(".") else "." + s) for s in OFFICIAL_SUFFIXES)


def _html_to_text(body: str) -> str:
    body = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", body)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    return re.sub(r"\s+", " ", html.unescape(body)).strip()


def fetch_official_source(session: Session, actor: Actor, url: str, *, job_id: int | None = None,
                          permit_id: int | None = None, find: str | None = None, edition: str | None = None,
                          client: httpx.Client | None = None) -> dict[str, Any]:
    """GET an allowlisted official page, keep passages, store the citation. Read-only; no
    query strings (prevents using the fetch as a data-exfiltration channel)."""
    require(actor, "write:documents")
    u = urlparse(url)
    if u.scheme != "https" or not u.hostname or not _is_official(u.hostname):
        raise Forbidden(f"research fetch is limited to official domains over https ({', '.join(OFFICIAL_SUFFIXES)})")
    if u.query or u.username or u.password or (u.port not in (None, 443)):
        raise Forbidden("query strings, credentials and non-standard ports are not allowed in research URLs")
    c = client or httpx.Client(timeout=httpx.Timeout(15), follow_redirects=False)
    try:
        r = c.get(url, headers={"User-Agent": "ConstructionHermes-research/0.1"})
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"fetch failed: {type(exc).__name__}", "url": url}
    if r.status_code in (301, 302, 303, 307, 308):
        return {"ok": False, "error": "redirect not followed; fetch the target URL directly if it is official",
                "location": r.headers.get("location")}
    if r.status_code != 200:
        return {"ok": False, "error": f"HTTP {r.status_code}", "url": url}
    raw = r.content[:3_000_000]
    ctype = r.headers.get("content-type", "")
    if "pdf" in ctype:
        stored = documents.store(session, actor, raw, filename=u.path.rsplit("/", 1)[-1] or "source.pdf", kind="other",
                                 title=f"Source: {url}", job_id=job_id)
        text_value = " ".join(p["text"] for p in session.get(documents.Document, stored["id"]).pages)
    else:
        text_value = _html_to_text(raw.decode(r.encoding or "utf-8", errors="replace"))
    title_m = re.search(r"(?is)<title>(.*?)</title>", raw.decode("utf-8", errors="replace")) if "html" in ctype else None
    title = html.unescape(title_m.group(1).strip())[:300] if title_m else url
    passages = []
    if find:
        for m in re.finditer(re.escape(find), text_value, re.I):
            passages.append(text_value[max(0, m.start() - 300): m.end() + 300])
            if len(passages) >= 3:
                break
    src = ResearchSource(job_id=job_id, permit_id=permit_id, url=url, title=title, publisher=u.hostname,
                         retrieved_at=timeutil.now(), edition=edition, passage="\n---\n".join(passages)[:5000] or None,
                         review_status="unreviewed", created_by_id=actor.user_id)
    session.add(src)
    session.flush()
    audit.record(session, actor, "research.fetch", "research", src.id, url=url)
    return {"ok": True, "source": ref("research", src.id), "title": title, "url": url,
            "retrieved_at": timeutil.iso(src.retrieved_at), "passages": passages,
            "note": "Untrusted page text. Cite URL and retrieval date; confirm edition/effective date and applicability."}

