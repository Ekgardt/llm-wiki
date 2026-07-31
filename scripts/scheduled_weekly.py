"""Weekly deep maintenance — runs Sunday 04:00 via Windows Task Scheduler.

What it does:
1. Everything the nightly pass does (drain + compile + lint).
2. OKF conformance sweep — backfills frontmatter on any new pages.
3. LLM-judged contradiction check (optional, opt-in via env var).
4. Report permanently-failed queue tasks for human review.

Designed to run unattended. Logs to $LLM_WIKI_STATE_ROOT/logs/weekly-YYYY-MM-DD.md.
"""

from __future__ import annotations

import os
import sys
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scheduled_nightly  # noqa: E402
from maintenance_helpers import run_step as _run_step  # noqa: E402
from maintenance_helpers import wait_for_compile_idle as _wait_for_compile_idle
from maintenance_helpers import write_console as _write_console
from memory_queue import status as queue_status  # noqa: E402
from memory_state import REPORTS_DIR, ROOT, STATE_ROOT, advisory_file_lock  # noqa: E402


def _rebuild_derived_indexes(log) -> int:
    return scheduled_nightly._rebuild_derived_indexes(log, include_markdown=True)


def main() -> int:
    maint_lock = STATE_ROOT / "run" / "maintenance.lock"
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = REPORTS_DIR / f"weekly-{today}.md"
    try:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _write_console(
            f"scheduled_weekly: ERROR: report setup failed ({type(exc).__name__}: {exc})",
            error=True,
        )
        return 1

    def log(msg: str) -> None:
        line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
        _write_console(line)
        with log_file.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    locks = ExitStack()
    try:
        locks.enter_context(
            advisory_file_lock(
                maint_lock,
                timeout=0,
                description="maintenance lock",
            )
        )
    except TimeoutError as exc:
        log(f"ERROR: maintenance lock contended: {exc}")
        return 1
    except OSError as exc:
        log(f"ERROR: maintenance lock unavailable ({type(exc).__name__}: {exc})")
        return 1

    with locks:
        log(f"=== Weekly deep maintenance — {today} ===")
        failures = 0

        # The weekly owns the lease; nightly must neither reacquire it nor build
        # indexes that the following page mutations would immediately stale.
        _wait_for_compile_idle(log)
        log("Step 1: drain queue + compile + structural lint...")
        try:
            rc = scheduled_nightly.main(acquire_lease=False, rebuild_indexes=False)
        except Exception as exc:  # noqa: BLE001
            log(f"ERROR: nightly maintenance raised {type(exc).__name__}: {exc}")
            failures += 1
        else:
            if rc:
                log(f"ERROR: nightly maintenance failed (rc={rc}).")
                failures += 1

        # Step 2: OKF conformance sweep — backfill missing frontmatter.
        log("Step 2: OKF conformance sweep (migrate_to_okf --apply)...")
        rc = _run_step(
            [sys.executable, str(ROOT / "scripts" / "migrate_to_okf.py"), "--apply"],
            log,
            "okf",
            timeout=120,
        )
        if rc:
            failures += 1

        # Exhausted tasks are durable evidence for human review. Report only
        # canonical task IDs; queue payloads may contain prompts or secrets.
        queue = queue_status()
        failed_count = int(queue.get("permanently_failed", 0))
        if failed_count:
            failed_ids = ", ".join(queue.get("permanently_failed_ids", []))
            id_detail = f" IDs: {failed_ids}." if failed_ids else " IDs unavailable."
            log(
                f"ERROR: {failed_count} exhausted queue task(s) require human review."
                f"{id_detail}"
            )
            failures += 1
        else:
            log("Step 3: no exhausted queue tasks require human review.")

        # Step 3b: auto-archive stale pages (>180 days).
        log("Step 3b: auto-archiving stale pages (>180 days)...")
        rc = _run_step(
            [
                sys.executable,
                str(ROOT / "scripts" / "archive_stale.py"),
                "--days",
                "180",
                "--apply",
            ],
            log,
            "archive",
            timeout=120,
        )
        if rc:
            failures += 1

        # Step 4: optional LLM-judged contradiction check.
        if os.environ.get("MEMORY_WEEKLY_CONTRADICTIONS", "").lower() in ("1", "true", "yes"):
            log("Step 4: LLM contradiction check (opt-in)...")
            rc = _run_step(
                [sys.executable, str(ROOT / "scripts" / "lint_memory.py"), "--contradictions"],
                log,
                "contradictions",
                timeout=1800,
            )
            if rc:
                failures += 1
        else:
            log(
                "Step 4: contradiction check SKIPPED (set MEMORY_WEEKLY_CONTRADICTIONS=1 to enable)"
            )

        log("Step 5: rebuilding final Markdown, FTS5, and graph indexes...")
        rebuild_failures = _rebuild_derived_indexes(log)
        if rebuild_failures:
            log(f"ERROR: final derived-index rebuild failed ({rebuild_failures} step(s)).")
            failures += rebuild_failures

        log(f"=== Weekly deep maintenance complete (failures={failures}) ===")
        return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
