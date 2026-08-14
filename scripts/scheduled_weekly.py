"""Weekly deep maintenance — runs Sunday 04:00 via Windows Task Scheduler.

What it does:
1. Everything the nightly pass does (queue work + compile + lint).
2. OKF conformance sweep — backfills frontmatter on any new pages.
3. LLM-judged contradiction check (optional, opt-in via env var).
4. Report queue status without deleting retained tasks.

Designed to run unattended. Logs to $LLM_WIKI_STATE_ROOT/logs/weekly-YYYY-MM-DD.md.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scheduled_nightly  # noqa: E402
from maintenance_helpers import run_step as _run_step  # noqa: E402
from maintenance_helpers import wait_for_compile_idle as _wait_for_compile_idle
from memory_state import REPORTS_DIR, ROOT  # noqa: E402
from operational_ownership import (  # noqa: E402
    OwnerLease,
    heartbeat_owner,
)


def _run_weekly_body(*, ownership: OwnerLease | None) -> int:
    if ownership is not None and (
        ownership.role != "weekly" or ownership.scope != "global"
    ):
        raise ValueError("weekly work requires a weekly global owner")

    def run_step(command, log, name, *, timeout):
        if ownership is not None and scheduled_nightly._accepts_ownership(_run_step):
            return _run_step(
                command,
                log,
                name,
                timeout=timeout,
                ownership=ownership,
            )
        return _run_step(command, log, name, timeout=timeout)
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
    log("Step 1: work queue + compile + structural lint...")
    rc = scheduled_nightly.run_nightly(ownership=ownership)
    if rc:
        failures += 1

    # Step 2: OKF conformance sweep — backfill missing frontmatter.
    log("Step 2: OKF conformance sweep (migrate_to_okf --apply)...")
    rc = run_step(
        [sys.executable, str(ROOT / "scripts" / "migrate_to_okf.py"), "--apply"],
        log, "okf", timeout=120,
    )
    if rc:
        failures += 1

    # Step 3: report retained queue tasks. Purge is always explicit and exported.
    log("Step 3: reporting memory queue status...")
    rc = run_step(
        [sys.executable, str(ROOT / "scripts" / "memory_queue.py"), "status"],
        log, "status", timeout=60,
    )
    if rc:
        failures += 1

    # Step 3b: auto-archive stale pages (>180 days).
    log("Step 3b: auto-archiving stale pages (>180 days)...")
    rc = run_step(
        [sys.executable, str(ROOT / "scripts" / "archive_stale.py"), "--days", "180", "--apply"],
        log, "archive", timeout=120,
    )
    if rc:
        failures += 1

    # Step 4: optional LLM-judged contradiction check.
    if os.environ.get("MEMORY_WEEKLY_CONTRADICTIONS", "").lower() in ("1", "true", "yes"):
        log("Step 4: LLM contradiction check (opt-in)...")
        rc = run_step(
            [sys.executable, str(ROOT / "scripts" / "lint_memory.py"), "--contradictions"],
            log, "contradictions", timeout=1800,
        )
        if rc:
            failures += 1
    else:
        log("Step 4: contradiction check SKIPPED (set MEMORY_WEEKLY_CONTRADICTIONS=1 to enable)")

    # Step 5: A-MEM reflection — consolidate pages with multiple updates (v4.0).
    log("Step 5: A-MEM reflection (page consolidation)...")
    try:
        from reflection import find_reflection_candidates, reflect_page
        candidates = find_reflection_candidates()
        if candidates:
            log(f"  Found {len(candidates)} reflection candidate(s)")
            for c in candidates:
                result = reflect_page(c["path"], apply=True)
                log(f"  {result}")
        else:
            log("  No reflection candidates found")
    except Exception as e:
        log(f"  reflection: failed ({e}) — skipping")

    # Step 6: generate L1 tier overviews (v4.0, best-effort).
    log("Step 6: generating L1 tier overviews...")
    try:
        from build_tiers import build_all_tiers
        stats = build_all_tiers(use_llm=False, verbose=False)
        log(f"  tiers: {stats['generated']} generated, {stats['skipped']} skipped")
    except Exception as e:
        log(f"  tiers: failed ({e}) — skipping")

    log(f"=== Weekly deep maintenance complete (failures={failures}) ===")
    return 1 if failures else 0


def run_weekly(*, ownership: OwnerLease | None) -> int:
    if ownership is None:
        return _run_weekly_body(ownership=None)
    with heartbeat_owner(ownership):
        return _run_weekly_body(ownership=ownership)


def main() -> int:
    marker = scheduled_nightly._acquire_legacy_maintenance_marker()
    if marker is None:
        print("scheduled_weekly: maintenance already running, skipping.", file=sys.stderr)
        return 0
    try:
        return run_weekly(ownership=None)
    finally:
        scheduled_nightly._release_legacy_maintenance_marker(marker)


if __name__ == "__main__":
    raise SystemExit(main())
