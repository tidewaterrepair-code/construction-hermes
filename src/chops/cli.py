"""Operator CLI. Runs locally on the server with direct database access (privileged path,
not reachable by the agent)."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys

from .authz import Actor


def _cli_owner(session) -> Actor:
    from sqlalchemy import select

    from .models import User

    u = session.scalar(select(User).where(User.role == "owner", User.active.is_(True)).limit(1))
    if u is None:
        sys.exit("no owner exists; run `chops bootstrap-owner` first")
    return Actor(user_id=u.id, role="owner", via="cli", display_name=f"{u.display_name} (cli)")


def cmd_migrate(args) -> None:
    from .config import get_settings
    from .migrate import current, upgrade

    url = get_settings().migrate_database_url or get_settings().database_url
    upgrade(url)
    print(f"schema at {current(url)}")


def cmd_bootstrap_owner(args) -> None:
    from .db import session_scope
    from .services import integrations, rates, users

    pw = os.environ.get("CHOPS_OWNER_PASSWORD") or getpass.getpass("Owner password (min 12 chars): ")
    with session_scope() as s:
        u = users.create_user(s, None, username=args.username, display_name=args.display_name, role="owner",
                              password=pw, bootstrap=True)
        integrations.ensure_rows(s)
        n = rates.load_example_assemblies(s)
        print(f"owner {u.username} created (USR-{u.id}); {n} example assemblies loaded")


def cmd_issue_agent_token(args) -> None:
    from sqlalchemy import select

    from .db import session_scope
    from .models import User
    from .services import users

    with session_scope() as s:
        owner = _cli_owner(s)
        agent = s.scalar(select(User).where(User.username == args.username))
        if agent is None:
            agent = users.create_user(s, owner, username=args.username, display_name="Construction Hermes", role="agent")
        if agent.role != "agent":
            sys.exit("refusing: that user is not an agent service identity")
        if args.revoke_existing:
            print(f"revoked {users.revoke_tokens(s, owner, agent.id)} token(s)")
        raw = users.issue_token(s, owner, agent.id, args.name, days=args.days)
    # Printed once; store it in HERMES_HOME/.env as CHOPS_MCP_TOKEN.
    if args.write_env:
        path = args.write_env
        lines = []
        if os.path.exists(path):
            lines = [ln for ln in open(path).read().splitlines() if not ln.startswith("CHOPS_MCP_TOKEN=")]
        lines.append(f"CHOPS_MCP_TOKEN={raw}")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        print(f"token written to {path} (mode 600)")
    else:
        print(raw)


def cmd_create_user(args) -> None:
    from .db import session_scope
    from .services import users

    pw = os.environ.get("CHOPS_NEW_USER_PASSWORD") or (getpass.getpass("Password: ") if args.role != "agent" else None)
    with session_scope() as s:
        u = users.create_user(s, _cli_owner(s), username=args.username, display_name=args.display_name, role=args.role,
                              password=pw)
        print(f"created USR-{u.id} {u.username} ({u.role})")


def cmd_assign_job(args) -> None:
    from sqlalchemy import select

    from .db import session_scope
    from .models import User
    from .refs import parse
    from .services import jobs

    with session_scope() as s:
        u = s.scalar(select(User).where(User.username == args.username.lower()))
        if u is None:
            sys.exit(f"no user {args.username}")
        print(jobs.assign_user(s, _cli_owner(s), parse(args.job, "job"), u.id, args.role))


def cmd_serve(args) -> None:
    import uvicorn

    uvicorn.run("chops.web:app", host=args.host, port=args.port, proxy_headers=True,
                forwarded_allow_ips=args.forwarded_allow_ips, log_level="info")


def cmd_serve_mcp(args) -> None:
    import uvicorn

    from .mcp_server import build_app

    hosts = [h.strip() for h in (args.allowed_hosts or "").split(",") if h.strip()] or None
    uvicorn.run(build_app(hosts), host=args.host, port=args.port, log_level="info")


def cmd_worker(args) -> None:
    from .worker import run_forever

    run_forever(once=args.once)


def cmd_seed_demo(args) -> None:
    from .config import get_settings
    from .db import session_scope
    from .demo import seed

    if get_settings().env == "prod":
        sys.exit("refusing to seed synthetic data with CHOPS_ENV=prod; use the demo database")
    with session_scope() as s:
        print(json.dumps(seed(s, _cli_owner(s)), indent=1))


def cmd_kill(args) -> None:
    from .db import session_scope
    from .services import settings

    with session_scope() as s:
        owner = _cli_owner(s)
        if args.action == "on":
            settings.engage_kill_switch(s, owner, args.reason or "engaged from CLI")
        elif args.action == "off":
            settings.release_kill_switch(s, owner, args.reason or "released from CLI")
        print(json.dumps(settings.kill_switch(s)))


def cmd_mode(args) -> None:
    from .db import session_scope
    from .services import settings

    with session_scope() as s:
        owner = _cli_owner(s)
        if args.mode:
            settings.set_mode(s, owner, args.mode, confirm_live=args.confirm_live)
        print(settings.mode(s))


def cmd_health(args) -> None:
    from .health import check

    res = check()
    print(json.dumps(res, indent=1))
    sys.exit(0 if res["ok"] else 1)


def cmd_backup(args) -> None:
    from .backup import create_backup

    print(json.dumps(create_backup(label=args.label), indent=1))


def cmd_restore_test(args) -> None:
    from .backup import restore_test

    res = restore_test(args.archive, keep=args.keep)
    print(json.dumps(res, indent=1))
    sys.exit(0 if res["ok"] else 1)


def cmd_export(args) -> None:
    from .db import session_scope
    from .services.export import export_zip

    with session_scope() as s:
        data = export_zip(s, _cli_owner(s), include_documents=not args.no_documents)
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    print(f"wrote {args.out} ({len(data)} bytes)")


def cmd_digest(args) -> None:
    from .db import session_scope
    from .services import digest

    with session_scope() as s:
        print(digest.render_text(digest.today(s, _cli_owner(s), include_synthetic=args.include_synthetic)))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="chops", description="Construction Hermes operations service")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate").set_defaults(fn=cmd_migrate)
    b = sub.add_parser("bootstrap-owner")
    b.add_argument("--username", default="jimmy")
    b.add_argument("--display-name", default="Jimmy Blackwell")
    b.set_defaults(fn=cmd_bootstrap_owner)
    t = sub.add_parser("issue-agent-token")
    t.add_argument("--username", default="hermes")
    t.add_argument("--name", default="hermes-gateway")
    t.add_argument("--days", type=int, default=365)
    t.add_argument("--revoke-existing", action="store_true")
    t.add_argument("--write-env", help="append CHOPS_MCP_TOKEN to this .env file (mode 600)")
    t.set_defaults(fn=cmd_issue_agent_token)
    u = sub.add_parser("create-user")
    u.add_argument("username")
    u.add_argument("--display-name", required=True)
    u.add_argument("--role", required=True, choices=["office", "foreman", "crew", "viewer", "agent"])
    u.set_defaults(fn=cmd_create_user)
    aj = sub.add_parser("assign-job")
    aj.add_argument("job")
    aj.add_argument("username")
    aj.add_argument("--role", default="foreman")
    aj.set_defaults(fn=cmd_assign_job)
    sv = sub.add_parser("serve")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8640)
    sv.add_argument("--forwarded-allow-ips", default="127.0.0.1")
    sv.set_defaults(fn=cmd_serve)
    sm = sub.add_parser("serve-mcp")
    sm.add_argument("--host", default="127.0.0.1")
    sm.add_argument("--port", type=int, default=8641)
    sm.add_argument("--allowed-hosts", default="")
    sm.set_defaults(fn=cmd_serve_mcp)
    w = sub.add_parser("worker")
    w.add_argument("--once", action="store_true")
    w.set_defaults(fn=cmd_worker)
    sub.add_parser("seed-demo").set_defaults(fn=cmd_seed_demo)
    k = sub.add_parser("kill-switch")
    k.add_argument("action", choices=["on", "off", "status"])
    k.add_argument("--reason")
    k.set_defaults(fn=cmd_kill)
    m = sub.add_parser("mode")
    m.add_argument("mode", nargs="?", choices=["BUILD", "SHADOW", "LIVE"])
    m.add_argument("--confirm-live", action="store_true")
    m.set_defaults(fn=cmd_mode)
    sub.add_parser("health").set_defaults(fn=cmd_health)
    bk = sub.add_parser("backup")
    bk.add_argument("--label", default="scheduled")
    bk.set_defaults(fn=cmd_backup)
    rt = sub.add_parser("restore-test")
    rt.add_argument("archive")
    rt.add_argument("--keep", action="store_true")
    rt.set_defaults(fn=cmd_restore_test)
    ex = sub.add_parser("export")
    ex.add_argument("--out", required=True)
    ex.add_argument("--no-documents", action="store_true")
    ex.set_defaults(fn=cmd_export)
    dg = sub.add_parser("digest")
    dg.add_argument("--include-synthetic", action="store_true")
    dg.set_defaults(fn=cmd_digest)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
