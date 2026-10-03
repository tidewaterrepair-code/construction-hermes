"""Full company data export: one CSV per table plus original documents, in a zip."""

from __future__ import annotations

import csv
import io
import json
import zipfile
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..authz import Actor, require
from ..models import ApiToken, Base, Document, RateLimitHit, WebSession
from . import audit, documents

EXCLUDE = {WebSession.__tablename__, ApiToken.__tablename__, RateLimitHit.__tablename__}
SECRET_COLUMNS = {"password_hash", "token_hash", "csrf_token", "id_hash"}


def _cell(v: Any) -> Any:
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str)
    return v


def export_zip(session: Session, actor: Actor, include_documents: bool = True) -> bytes:
    require(actor, "export:all")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for table in Base.metadata.sorted_tables:
            if table.name in EXCLUDE:
                continue
            cols = [c.name for c in table.columns if c.name not in SECRET_COLUMNS and c.name != "search_vector"]
            out = io.StringIO()
            w = csv.writer(out)
            w.writerow(cols)
            for row in session.execute(select(*[table.c[c] for c in cols]).order_by(*table.primary_key.columns)):
                w.writerow([_cell(v) for v in row])
            zf.writestr(f"tables/{table.name}.csv", out.getvalue())
        if include_documents:
            for d in session.scalars(select(Document)):
                p = documents.storage_path(d.sha256)
                if p.exists():
                    zf.write(p, f"documents/{d.id}-{d.original_filename}")
        zf.writestr("README.txt", "Construction Hermes export. tables/*.csv are UTF-8 CSV (JSON columns as JSON text). "
                                  "documents/ holds original uploaded and generated files named <id>-<filename>. "
                                  "Secrets (password/token hashes, sessions) are excluded.\n")
    audit.record(session, actor, "export.all", None, None, documents=include_documents)
    return buf.getvalue()

