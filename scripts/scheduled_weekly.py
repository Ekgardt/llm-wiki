"""Weekly deep maintenance — runs Sunday 04:00 via Windows Task Scheduler.

What it does:
1. Everything the nightly pass does (drain + compile + lint).
2. OKF conformance sweep — backfills frontmatter on any new pages.
3. LLM-judged contradiction check (optional, opt-in via env var).
4. Prune permanently-failed queue tasks.

Designed to run unattended. Logs to $LLM_WIKI_STATE_ROOT\\logs\\weekly-YYYY-MM-DD.md.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import REPORTS_DIR, ROOT  # noqa: E402


def main() -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = REPORTS_DIR / f"weekly-{today}.md"
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    def log(msg: str) -> None:
        line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
        print(line)
        with log_file.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    log(f"=== Weekly deep maintenance — {today} ===")
    failures = 0

    import subprocess

    # Step 1: full nightly-style pass.
    log("Step 1: drain queue + compile + structural lint...")
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "scheduled_nightly.py")],
        cwd=str(ROOT), capture_output=True, text=True, timeout=1800,
    )
    for line in r.stdout.splitlines():
        log(f"  nightly: {line}")
    if r.returncode != 0:
        failures += 1

    # Step 2: OKF conformance sweep — backfill missing frontmatter.
    log("Step 2: OKF conformance sweep (migrate_to_okf --apply)...")
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "migrate_to_okf.py"), "--apply"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=120,
    )
    for line in r.stdout.splitlines()[-6:]:
        log(f"  okf: {line}")
    if r.returncode != 0:
        failures += 1

    # Step 3: prune permanently-failed queue tasks (attempts >= 5).
    log("Step 3: pruning permanently-failed queue tasks...")
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "memory_queue.py"), "clear-failed"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=60,
    )
    log(f"  prune: {r.stdout.strip()}")
    if r.returncode != 0:
        failures += 1

    # Step 3b: auto-archive stale pages (>180 days).
    log("Step 3b: auto-archiving stale pages (>180 days)...")
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "archive_stale.py"), "--days", "180", "--apply"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=120,
    )
    for line in r.stdout.splitlines()[-5:]:
        log(f"  archive: {line}")
    if r.returncode != 0:
        failures += 1

    # Step 4: optional LLM-judged contradiction check.
    if os.environ.get("MEMORY_WEEKLY_CONTRADICTIONS", "").lower() in ("1", "true", "yes"):
        log("Step 4: LLM contradiction check (opt-in)...")
        r = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "lint_memory.py"), "--contradictions"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=1800,
        )
        for line in r.stdout.splitlines()[-5:]:
            log(f"  contradictions: {line}")
        if r.returncode != 0:
            failures += 1
    else:
        log("Step 4: contradiction check SKIPPED (set MEMORY_WEEKLY_CONTRADICTIONS=1 to enable)")

    log(f"=== Weekly deep maintenance complete (failures={failures}) ===")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
