"""Weekly deep maintenance — runs Sunday 04:00 via Windows Task Scheduler.

What it does:
1. Everything the nightly pass does (drain + compile + lint).
2. OKF conformance sweep — backfills frontmatter on any new pages.
3. LLM-judged contradiction check (optional, opt-in via env var).
4. Prune permanently-failed queue tasks.

Designed to run unattended. Logs to $LLM_WIKI_STATE_ROOT/logs/weekly-YYYY-MM-DD.md.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from maintenance_helpers import run_step as _run_step  # noqa: E402
from maintenance_helpers import wait_for_compile_idle as _wait_for_compile_idle
from memory_state import REPORTS_DIR, ROOT, STATE_ROOT  # noqa: E402


def main() -> int:
    # Maintenance lease: serialize weekly against concurrent triggers and
    # cover the migrate/archive steps that run after the nightly subprocess
    # returns (nightly manages its own lock for step 1 — we yield around it).
    maint_lock = STATE_ROOT / "run" / "maintenance.lock"
    maint_lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(maint_lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        print("scheduled_weekly: maintenance already running, skipping.", file=sys.stderr)
        return 0

    try:
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

        # Step 1: full nightly-style pass.
        _wait_for_compile_idle(log)
        log("Step 1: drain queue + compile + structural lint...")
        # Yield the lock so the nightly subprocess can acquire it for
        # drain/compile/lint; re-acquire afterwards to cover migrate/archive.
        try:
            maint_lock.unlink()
        except OSError:
            pass
        rc = _run_step(
            [sys.executable, str(ROOT / "scripts" / "scheduled_nightly.py")],
            log, "nightly", timeout=1800,
        )
        if rc:
            failures += 1
        # Re-acquire for the remaining maintenance steps. If a concurrent
        # run grabbed it during the window, bail out of the remaining steps.
        try:
            fd = os.open(str(maint_lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
        except FileExistsError:
            log("scheduled_weekly: maintenance lock grabbed during step 1 — aborting remaining steps.")
            return 1 if failures else 0

        # Step 2: OKF conformance sweep — backfill missing frontmatter.
        log("Step 2: OKF conformance sweep (migrate_to_okf --apply)...")
        rc = _run_step(
            [sys.executable, str(ROOT / "scripts" / "migrate_to_okf.py"), "--apply"],
            log, "okf", timeout=120,
        )
        if rc:
            failures += 1

        # Step 3: prune permanently-failed queue tasks (attempts >= 5).
        log("Step 3: pruning permanently-failed queue tasks...")
        rc = _run_step(
            [sys.executable, str(ROOT / "scripts" / "memory_queue.py"), "clear-failed"],
            log, "prune", timeout=60,
        )
        if rc:
            failures += 1

        # Step 3b: auto-archive stale pages (>180 days).
        log("Step 3b: auto-archiving stale pages (>180 days)...")
        rc = _run_step(
            [sys.executable, str(ROOT / "scripts" / "archive_stale.py"), "--days", "180", "--apply"],
            log, "archive", timeout=120,
        )
        if rc:
            failures += 1

        # Step 4: optional LLM-judged contradiction check.
        if os.environ.get("MEMORY_WEEKLY_CONTRADICTIONS", "").lower() in ("1", "true", "yes"):
            log("Step 4: LLM contradiction check (opt-in)...")
            rc = _run_step(
                [sys.executable, str(ROOT / "scripts" / "lint_memory.py"), "--contradictions"],
                log, "contradictions", timeout=1800,
            )
            if rc:
                failures += 1
        else:
            log("Step 4: contradiction check SKIPPED (set MEMORY_WEEKLY_CONTRADICTIONS=1 to enable)")

        log(f"=== Weekly deep maintenance complete (failures={failures}) ===")
        return 1 if failures else 0
    finally:
        try:
            maint_lock.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
