"""Weekly deep maintenance — runs Sunday 04:00 via Windows Task Scheduler.

What it does:
1. Everything the nightly pass does (queue work + compile + lint).
2. OKF conformance sweep — backfills frontmatter on any new pages.
3. Retention — stale pages, session records, and the superseded evidence-graph
   generations nothing reads any more (`prune_generations.py`).
4. LLM-judged contradiction check (optional, opt-in via env var).
5. Report queue status without deleting retained tasks.

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


def _script_steps() -> list[tuple[str, str, list[str], int]]:
    """(message, label, command, timeout) for every subprocess step, in order."""
    script = ROOT / "scripts"
    return [
        (
            "Step 2: OKF conformance sweep (migrate_to_okf --apply)...",
            "okf",
            [sys.executable, str(script / "migrate_to_okf.py"), "--apply"],
            120,
        ),
        (
            "Step 3: reporting memory queue status...",
            "status",
            [sys.executable, str(script / "memory_queue.py"), "status"],
            60,
        ),
        (
            "Step 3b: auto-archiving stale pages (>180 days)...",
            "archive",
            [sys.executable, str(script / "archive_stale.py"), "--days", "180", "--apply"],
            120,
        ),
        (
            "Step 3c: archiving session records (>90 days)...",
            "sessions",
            [sys.executable, str(script / "archive_sessions.py"), "--apply"],
            300,
        ),
        (
            "Step 3d: pruning superseded evidence-graph generations...",
            "generations",
            [sys.executable, str(script / "prune_generations.py"), "--apply"],
            1200,
        ),
    ]


def _contradictions_wanted() -> bool:
    return os.environ.get("MEMORY_WEEKLY_CONTRADICTIONS", "").lower() in (
        "1",
        "true",
        "yes",
    )


def _require_weekly_owner(ownership: OwnerLease | None) -> None:
    if ownership is None:
        return
    if ownership.role != "weekly" or ownership.scope != "global":
        raise ValueError("weekly work requires a weekly global owner")


def _step_runner(ownership: OwnerLease | None):
    """Run one subprocess step, carrying the lease when the helper takes one."""

    def run_step(command, log, name, *, timeout):
        if ownership is not None and scheduled_nightly._accepts_ownership(_run_step):
            return _run_step(command, log, name, timeout=timeout, ownership=ownership)
        return _run_step(command, log, name, timeout=timeout)

    return run_step


def _logger(log_file: Path):
    def log(msg: str) -> None:
        line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
        print(line)
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    return log


def _run_script_steps(run_step, log) -> int:
    failures = 0
    for message, label, command, timeout in _script_steps():
        log(message)
        failures += int(bool(run_step(command, log, label, timeout=timeout)))
    return failures


def _run_contradictions(run_step, log) -> int:
    if not _contradictions_wanted():
        log("Step 4: contradiction check SKIPPED (set MEMORY_WEEKLY_CONTRADICTIONS=1 to enable)")
        return 0
    log("Step 4: LLM contradiction check (opt-in)...")
    command = [sys.executable, str(ROOT / "scripts" / "lint_memory.py"), "--contradictions"]
    return int(bool(run_step(command, log, "contradictions", timeout=1800)))


def _reflect(log) -> None:
    """A-MEM reflection — consolidate pages with multiple updates (v4.0)."""
    log("Step 5: A-MEM reflection (page consolidation)...")
    try:
        _reflect_candidates(log)
    except Exception as error:  # noqa: BLE001 - best effort by design
        log(f"  reflection: failed ({error}) — skipping")


def _reflect_candidates(log) -> None:
    from reflection import find_reflection_candidates, reflect_page

    candidates = find_reflection_candidates()
    if not candidates:
        log("  No reflection candidates found")
        return
    log(f"  Found {len(candidates)} reflection candidate(s)")
    for candidate in candidates:
        log(f"  {reflect_page(candidate['path'], apply=True)}")


def _build_tiers(log) -> None:
    """Generate L1 tier overviews (v4.0, best-effort)."""
    log("Step 6: generating L1 tier overviews...")
    try:
        from build_tiers import build_all_tiers

        stats = build_all_tiers(use_llm=False, verbose=False)
        log(f"  tiers: {stats['generated']} generated, {stats['skipped']} skipped")
    except Exception as error:  # noqa: BLE001 - best effort by design
        log(f"  tiers: failed ({error}) — skipping")


def _run_weekly_body(*, ownership: OwnerLease | None) -> int:
    _require_weekly_owner(ownership)
    run_step = _step_runner(ownership)
    today = datetime.now().strftime("%Y-%m-%d")
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    log = _logger(REPORTS_DIR / f"weekly-{today}.md")

    log(f"=== Weekly deep maintenance — {today} ===")
    _wait_for_compile_idle(log)
    log("Step 1: work queue + compile + structural lint...")
    failures = int(bool(scheduled_nightly.run_nightly(ownership=ownership)))
    failures += _run_script_steps(run_step, log)
    failures += _run_contradictions(run_step, log)
    _reflect(log)
    _build_tiers(log)
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
