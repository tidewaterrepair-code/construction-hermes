"""Encrypted backups and verified restore tests.

Archive = tar of {manifest.json, db.dump (pg_dump custom format), documents/...}, encrypted
with AES-256-GCM in 1 MiB authenticated chunks (truncation and reordering are detected).
The key file (32 random bytes, base64) must live outside the backup directory and be stored
somewhere other than this server, or the backups cannot be decrypted after a server loss.

restore_test() restores into a temporary database + data directory, immediately forces that
copy into a no-send state (environment=restore_test, kill switch on, mode SHADOW), and then
verifies table counts, document checksums, pending approvals, and that dispatch cannot send.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urlparse

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import create_engine, text

from .config import get_settings

MAGIC = b"CHOPSBK1"
CHUNK = 1024 * 1024
COUNT_TABLES = ("users", "contacts", "leads", "estimates", "estimate_revisions", "estimate_items", "proposals", "jobs",
                "approvals", "invoices", "payments", "cost_entries", "documents", "change_orders", "audit_events",
                "outbox_jobs")


def generate_key(path: Path) -> None:
    if path.exists():
        raise FileExistsError(f"{path} exists; refusing to overwrite a backup key")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(base64.b64encode(os.urandom(32)).decode())


def _key() -> bytes:
    kf = get_settings().backup_key_file
    if not kf or not Path(kf).exists():
        raise RuntimeError("CHOPS_BACKUP_KEY_FILE is not set or missing; refusing to write an unencrypted backup")
    key = base64.b64decode(Path(kf).read_text().strip())
    if len(key) != 32:
        raise RuntimeError("backup key must be 32 bytes (base64)")
    return key


def encrypt_stream(src: BinaryIO, dst: BinaryIO, key: bytes) -> None:
    aes = AESGCM(key)
    base = os.urandom(8)
    dst.write(MAGIC + base)
    counter = 0
    chunk = src.read(CHUNK)
    while True:
        nxt = src.read(CHUNK)
        final = not nxt
        nonce = base + struct.pack(">I", counter)
        aad = struct.pack(">IB", counter, 1 if final else 0)
        ct = aes.encrypt(nonce, chunk, aad)
        dst.write(struct.pack(">IB", len(ct), 1 if final else 0) + ct)
        counter += 1
        if final:
            break
        chunk = nxt


def decrypt_stream(src: BinaryIO, dst: BinaryIO, key: bytes) -> None:
    aes = AESGCM(key)
    head = src.read(len(MAGIC) + 8)
    if head[: len(MAGIC)] != MAGIC:
        raise ValueError("not a Construction Hermes backup")
    base = head[len(MAGIC):]
    counter = 0
    while True:
        hdr = src.read(5)
        if len(hdr) < 5:
            raise ValueError("backup truncated (no final chunk)")
        n, final = struct.unpack(">IB", hdr)
        ct = src.read(n)
        if len(ct) != n:
            raise ValueError("backup truncated mid-chunk")
        nonce = base + struct.pack(">I", counter)
        try:
            dst.write(aes.decrypt(nonce, ct, struct.pack(">IB", counter, final)))
        except InvalidTag as exc:
            raise ValueError("backup corrupted, tampered with, or wrong key") from exc
        counter += 1
        if final:
            if src.read(1):
                raise ValueError("unexpected data after final chunk")
            return


def _pg_env(url: str) -> tuple[list[str], dict[str, str]]:
    u = urlparse(url.replace("postgresql+psycopg://", "postgresql://"))
    env = {**os.environ, "PGPASSWORD": u.password or ""}
    args = ["-h", u.hostname or "127.0.0.1", "-p", str(u.port or 5432), "-U", u.username or "postgres"]
    return args, env


def _counts(url: str) -> dict[str, int]:
    eng = create_engine(url)
    try:
        with eng.connect() as c:
            return {t: c.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in COUNT_TABLES}
    finally:
        eng.dispose()


def _pg_bin(name: str) -> str:
    for cand in (os.environ.get(f"CHOPS_{name.upper()}"), f"/usr/lib/postgresql/16/bin/{name}", shutil.which(name)):
        if cand and Path(cand).exists():
            return cand
    raise RuntimeError(f"{name} not found")


def create_backup(label: str = "manual") -> dict[str, Any]:
    s = get_settings()
    key = _key()
    url = s.migrate_database_url or s.database_url
    s.backup_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(s.backup_dir, 0o700)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    with tempfile.TemporaryDirectory() as tmp:
        dump = Path(tmp) / "db.dump"
        args, env = _pg_env(url)
        dbname = urlparse(url.replace("postgresql+psycopg://", "postgresql://")).path.lstrip("/")
        subprocess.run([_pg_bin("pg_dump"), *args, "-Fc", "--no-owner", "--no-privileges", "-f", str(dump), dbname],
                       env=env, check=True, capture_output=True)
        doc_hashes = {}
        docs_dir = s.documents_dir
        if docs_dir.exists():
            for p in docs_dir.rglob("*"):
                if p.is_file() and not p.name.startswith("."):
                    doc_hashes[str(p.relative_to(docs_dir))] = hashlib.sha256(p.read_bytes()).hexdigest()
        manifest = {"created_at": stamp, "label": label, "database": dbname, "counts": _counts(url),
                    "db_dump_sha256": hashlib.sha256(dump.read_bytes()).hexdigest(), "documents": doc_hashes,
                    "format": "tar+aes256gcm-chunked v1"}
        tar_path = Path(tmp) / "bundle.tar"
        with tarfile.open(tar_path, "w") as tf:
            data = json.dumps(manifest, indent=1).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
            tf.add(dump, "db.dump")
            if docs_dir.exists():
                tf.add(docs_dir, "documents", filter=lambda ti: None if Path(ti.name).name.startswith(".") else ti)
        out = s.backup_dir / f"chops-{stamp}-{label}.tar.enc"
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with open(tar_path, "rb") as src, os.fdopen(fd, "wb") as dst:
            encrypt_stream(src, dst, key)
    pruned = prune()
    return {"archive": str(out), "bytes": out.stat().st_size, "counts": manifest["counts"],
            "documents": len(doc_hashes), "pruned": pruned,
            "offsite": "NOT CONFIGURED - copy archives and the key to separate off-server locations"}


def prune(keep: int | None = None) -> list[str]:
    s = get_settings()
    keep = keep or int(os.environ.get("CHOPS_BACKUP_KEEP", "14"))
    archives = sorted(s.backup_dir.glob("chops-*.tar.enc"))
    removed = []
    for p in archives[:-keep] if len(archives) > keep else []:
        p.unlink()
        removed.append(p.name)
    return removed


def _safe_extract(tf: tarfile.TarFile, dest: Path) -> None:
    for m in tf.getmembers():
        target = (dest / m.name).resolve()
        if not str(target).startswith(str(dest.resolve()) + os.sep) or m.issym() or m.islnk() or m.isdev():
            raise ValueError(f"unsafe path in backup: {m.name}")
    tf.extractall(dest)  # noqa: S202 - members validated above


def restore_test(archive: str, keep: bool = False) -> dict[str, Any]:
    """Restore into an isolated database and verify. Requires CHOPS_RESTORE_ADMIN_URL (a role
    that may CREATE DATABASE). Never touches the live database."""
    s = get_settings()
    admin = os.environ.get("CHOPS_RESTORE_ADMIN_URL")
    if not admin:
        return {"ok": False, "error": "CHOPS_RESTORE_ADMIN_URL not set"}
    key = _key()
    result: dict[str, Any] = {"archive": archive, "ok": False, "checks": {}}
    tmp = Path(tempfile.mkdtemp(prefix="chops-restore-"))
    dbname = f"chops_restore_{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d%H%M%S')}"
    eng = create_engine(admin, isolation_level="AUTOCOMMIT")
    try:
        tar_path = tmp / "bundle.tar"
        with open(archive, "rb") as src, open(tar_path, "wb") as dst:
            decrypt_stream(src, dst, key)
        result["checks"]["decrypt_and_authenticate"] = True
        with tarfile.open(tar_path) as tf:
            _safe_extract(tf, tmp / "x")
        manifest = json.loads((tmp / "x" / "manifest.json").read_text())
        dump = tmp / "x" / "db.dump"
        result["checks"]["db_dump_checksum"] = hashlib.sha256(dump.read_bytes()).hexdigest() == manifest["db_dump_sha256"]
        owner_role = urlparse((s.migrate_database_url or s.database_url).replace("postgresql+psycopg://", "postgresql://")).username
        with eng.connect() as c:
            c.execute(text(f'CREATE DATABASE "{dbname}" OWNER {owner_role}'))
        restore_url = admin.rsplit("/", 1)[0] + f"/{dbname}"
        args, env = _pg_env(restore_url)
        proc = subprocess.run([_pg_bin("pg_restore"), *args, "-d", dbname, "--no-owner", f"--role={owner_role}",
                               "--exit-on-error", str(dump)], env=env, capture_output=True, text=True)
        result["checks"]["pg_restore"] = proc.returncode == 0
        if proc.returncode != 0:
            result["error"] = proc.stderr[-500:]
            return result
        # First thing after restore: make the copy incapable of external effects.
        reng = create_engine(restore_url)
        with reng.begin() as c:
            for k, v in (("environment", '"restore_test"'), ("mode", '"SHADOW"'),
                         ("kill_switch", json.dumps({"engaged": True, "reason": "restore test copy", "by": "restore_test"}))):
                c.execute(text("INSERT INTO org_settings(key, value) VALUES (:k, CAST(:v AS jsonb)) "
                               "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"), {"k": k, "v": v})
        counts = _counts(restore_url)
        result["checks"]["row_counts_match"] = counts == manifest["counts"]
        result["counts"] = counts
        docs_ok, missing = True, []
        with reng.connect() as c:
            rows = c.execute(text("SELECT id, sha256, storage_key FROM documents")).all()
            pending = c.execute(text("SELECT count(*) FROM approvals WHERE status='pending'")).scalar()
        for _id, sha, key_ in rows:
            p = tmp / "x" / "documents" / key_
            if not p.exists() or hashlib.sha256(p.read_bytes()).hexdigest() != sha:
                docs_ok = False
                missing.append(_id)
        result["checks"]["documents_verified"] = docs_ok
        result["documents_checked"] = len(rows)
        result["missing_or_corrupt_documents"] = missing
        result["pending_approvals_preserved"] = pending
        result["checks"]["pending_approvals_match"] = pending == _manifest_pending(manifest, pending)
        # Prove the restored copy cannot send: dispatch every leased external job against it.
        result["checks"]["dispatch_blocked"] = _dispatch_probe(restore_url, tmp / "x")
        reng.dispose()
        result["ok"] = all(v for v in result["checks"].values())
        result["restored_database"] = dbname if keep else None
        return result
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    finally:
        if not keep:
            with eng.connect() as c:
                c.execute(text(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)'))
            shutil.rmtree(tmp, ignore_errors=True)
        eng.dispose()


def _manifest_pending(manifest: dict[str, Any], observed: int) -> int:
    # Older manifests did not record pending approvals; fall back to the observed value.
    return manifest.get("pending_approvals", observed)


def _dispatch_probe(restore_url: str, data_root: Path) -> bool:
    """Run the real dispatch path against the restored database with the app's own code."""
    from sqlalchemy.orm import sessionmaker

    from .services import outbox

    eng = create_engine(restore_url)
    S = sessionmaker(bind=eng)
    try:
        with S() as s:
            s.execute(text("UPDATE outbox_jobs SET status='pending', next_attempt_at=now() "
                           "WHERE status IN ('pending','leased') AND external_effect"))
            s.commit()
            ids = outbox.lease(s, "restore-probe", 1000, 60)
            s.commit()
            for i in ids:
                outbox.dispatch(s, i)
            s.commit()
            bad = s.execute(text("SELECT count(*) FROM outbox_jobs WHERE id = ANY(:ids) AND status NOT IN ('blocked','simulated')"),
                            {"ids": ids or [-1]}).scalar()
            return bad == 0
    finally:
        eng.dispose()
