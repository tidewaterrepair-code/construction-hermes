"""Host usage guard: parses Hermes insights, ignores cache re-reads, warns the owner before stopping."""
import os
import shutil
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

INSIGHTS = """
  📋 Overview
  Sessions:          3             Messages:        41
  Tool calls:        12            User messages:   9
  Input tokens:      {inp:<12,}  Output tokens:   {out:,}
  Total tokens:      {total:,}
"""


def _exe(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _run(tmp_path, inp, out, cache, cap, telegram=True):
    deploy = tmp_path / "deploy"
    (deploy / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "deploy/scripts/usage-guard.sh", deploy / "scripts/usage-guard.sh")
    if telegram:
        (deploy / "hermes.env").write_text("TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_ALLOWED_USERS=555, 777\n")
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    log = tmp_path / "calls.log"
    (tmp_path / "insights.txt").write_text(INSIGHTS.format(inp=inp, out=out, total=inp + out + cache))
    _exe(bin_ / "docker", f'echo "docker $*" >>{log}\n'
         f'case "$*" in *insights*) cat {tmp_path / "insights.txt"};; esac\n')
    _exe(bin_ / "curl", f'echo "curl $* config=$(cat)" >>{log}\n')
    env = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}", CHOPS_MONTHLY_TOKEN_CAP=str(cap))
    p = subprocess.run(["bash", str(deploy / "scripts/usage-guard.sh")], env=env,
                       capture_output=True, text=True, timeout=30)
    return p, (log.read_text() if log.exists() else "")


def test_cache_rereads_do_not_trip_the_cap(tmp_path):
    p, calls = _run(tmp_path, inp=300_000, out=20_000, cache=5_000_000, cap=2_000_000)
    assert p.returncode == 0, p.stderr
    assert "tokens=320000" in p.stdout
    assert "stop hermes" not in calls and "curl" not in calls


def test_over_cap_notifies_owner_then_stops(tmp_path):
    p, calls = _run(tmp_path, inp=1_900_000, out=200_000, cache=0, cap=2_000_000)
    assert p.returncode == 0, p.stderr
    lines = calls.splitlines()
    curl = next(i for i, l in enumerate(lines) if l.startswith("curl"))
    stop = next(i for i, l in enumerate(lines) if "stop hermes" in l)
    assert curl < stop
    assert "chat_id=555" in lines[curl] and "123:abc" not in lines[curl].split("config=")[0]
    assert "kill-switch on" in calls


def test_unreadable_usage_leaves_services_alone(tmp_path):
    deploy = tmp_path / "deploy"
    (deploy / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "deploy/scripts/usage-guard.sh", deploy / "scripts/usage-guard.sh")
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    _exe(bin_ / "docker", "exit 1\n")
    env = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}")
    p = subprocess.run(["bash", str(deploy / "scripts/usage-guard.sh")], env=env,
                       capture_output=True, text=True, timeout=30)
    assert p.returncode == 2
