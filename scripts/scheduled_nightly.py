"""Nightly consolidation — runs at 03:00 via Windows Task Scheduler.

What it does:
1. Work any pending queue tasks (deferred LLM work).
2. Force-spawn compile to process all uncompiled daily logs.
3. Run lint and bounded immutable generation maintenance.

Designed to be invoked by Task Scheduler; never requires user interaction.
All output goes to $LLM_WIKI_STATE_ROOT/logs/nightly-YYYY-MM-DD.md.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import maybe_compile  # noqa: E402
from doctor import (  # noqa: E402
    DEFAULT_GENERATION_SOURCE_LIMIT,
    run_generation_maintenance,
)
from maintenance_helpers import run_step as _run_step  # noqa: E402
from maintenance_helpers import wait_for_compile_idle as _wait_for_compile_idle
from memory_state import (  # noqa: E402
    REPORTS_DIR,
    ROOT,
    STATE_ROOT,
    _is_pid_alive,
    update_state,
)


def _refresh_generation(log) -> int:
    """Run the shared bounded builder under its fenced maintenance owner."""
    result = run_generation_maintenance(
        root=ROOT,
        state_root=STATE_ROOT,
        time_budget_seconds=60,
        max_sources=DEFAULT_GENERATION_SOURCE_LIMIT,
    )
    status = result["status"]
    generation = result.get("generation_id") or "none"
    log(f"  generation: {status} (id={generation}, partial={bool(result.get('partial'))})")
    return 1 if status == "error" else 0


def _record_nightly_result(today: str, failures: int, error: str | None = None) -> None:
    """Release today's catchup lease and persist the terminal result."""
    timestamp = datetime.now().isoformat(timespec="seconds")

    def _mutate(state: dict) -> None:
        claim = state.get("nightly_catchup_claim", {})
        if claim.get("date") == today:
            state.pop("nightly_catchup_claim", None)
        if failures:
            state["last_nightly_status"] = "failed"
            state["last_nightly_failure"] = {
                "date": today,
                "failed_at": timestamp,
                "failures": failures,
                **({"error": error} if error else {}),
            }
        else:
            state["last_nightly_status"] = "success"
            state["last_nightly_date"] = today
            state.pop("last_nightly_failure", None)

    update_state(_mutate)


def _record_nightly_skip(today: str, reason: str) -> None:
    """Release today's claim without replacing the last execution result."""
    timestamp = datetime.now().isoformat(timespec="seconds")

    def _mutate(state: dict) -> None:
        claim = state.get("nightly_catchup_claim", {})
        if claim.get("date") == today:
            state.pop("nightly_catchup_claim", None)
        state["last_nightly_skip"] = {
            "date": today,
            "skipped_at": timestamp,
            "status": "deferred",
            "reason": reason,
        }

    update_state(_mutate)


def main() -> int:
    today = datetime.now().strftime("%Y-%m-%d")

    def _skip_for_maintenance(message: str) -> int:
        print(message, file=sys.stderr)
        try:
            _record_nightly_skip(today, "maintenance_lock_held")
        except Exception as exc:
            print(f"scheduled_nightly: could not record skip: {exc}", file=sys.stderr)
        return 0

    # Maintenance lease: prevent concurrent nightly/weekly runs.
    maint_lock = STATE_ROOT / "run" / "maintenance.lock"
    maint_lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(maint_lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        # Check if the lock is stale (older than 30 minutes) and the
        # holder PID is dead. If so, steal it; otherwise skip.
        stolen = False
        try:
            age = time.time() - maint_lock.stat().st_mtime
            if age > 1800:  # 30 minutes
                try:
                    old_pid = int(maint_lock.read_text(encoding="utf-8").strip())
                    if not _is_pid_alive(old_pid):
                        maint_lock.unlink()
                        # Retry acquisition
                        fd = os.open(str(maint_lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                        os.write(fd, str(os.getpid()).encode())
                        os.close(fd)
                        stolen = True
                    else:
                        return _skip_for_maintenance(
                            "scheduled_nightly: maintenance running (stale but PID alive), skipping."
                        )
                except (ValueError, OSError):
                    maint_lock.unlink()
        except OSError:
            pass
        if not stolen:
            return _skip_for_maintenance(
                "scheduled_nightly: maintenance already running, skipping."
            )

    failures = 1
    terminal_error = None
    try:
        log_file = REPORTS_DIR / f"nightly-{today}.md"
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)

        def log(msg: str) -> None:
            line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
            print(line)
            with log_file.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

        log(f"=== Nightly consolidation pass — {today} ===")
        failures = 0

        # Step 1: run the bounded deferred queue worker.
        log("Step 1: working deferred memory queue...")
        rc = _run_step(
            [sys.executable, str(ROOT / "scripts" / "memory_queue.py"), "work"],
            log,
            "work",
            timeout=600,
        )
        if rc:
            failures += 1

        # Step 2: maybe_compile (will spawn compile if there's pending work).
        _wait_for_compile_idle(log)
        log("Step 2: triggering compile (if needed)...")
        rc = _run_step(
            [sys.executable, str(ROOT / "scripts" / "maybe_compile.py")],
            log,
            "maybe_compile",
            timeout=60,
        )
        if rc:
            failures += 1

        # Step 2b: wait for compile to finish before rebuilding indexes.
        log("Step 2b: waiting for compile to finish...")
        compile_still_running = False
        for _ in range(60):  # max 5 minutes (60 × 5s)
            try:
                st = maybe_compile.status()
                if not st["compile_running"]:
                    break
            except Exception:
                break
            time.sleep(5)
        else:
            compile_still_running = True

        if compile_still_running:
            log("WARNING: compile still running after 5 min — skipping lint/index/graph")
            # Skip steps 3, 3b, 3c — they depend on compile output
            failures += 1
        else:
            # Step 3: structural lint (cheap, no LLM).
            log("Step 3: structural lint...")
            rc = _run_step(
                [sys.executable, str(ROOT / "scripts" / "lint_memory.py")],
                log,
                "lint",
                timeout=120,
            )
            if rc:
                failures += 1

            # Step 3b: rebuild FTS5 search index (cheap, no LLM, <1s for 100 pages).
            log("Step 3b: rebuilding FTS5 search index...")
            rc = _run_step(
                [sys.executable, str(ROOT / "scripts" / "search_memory.py"), "--rebuild"],
                log,
                "search",
                timeout=60,
            )
            if rc:
                failures += 1

            # Step 3c: refresh one immutable generation under the shared fence.
            log("Step 3c: refreshing immutable evidence generation...")
            failures += _refresh_generation(log)

            # Step 3d: compact disposable telemetry without touching knowledge.
            log("Step 3d: compacting retrieval telemetry...")
            try:
                from retrieval_telemetry import compact

                compacted = compact()
                log(f"  telemetry: compacted {compacted} event(s)")
            except Exception as e:
                log(f"  telemetry: failed ({e}) — skipping")

        # Step 4: prune old nightly logs (>30 days).
        log("Step 4: pruning old nightly reports...")
        pruned = 0
        cutoff = datetime.now().timestamp() - (30 * 86400)
        for p in REPORTS_DIR.glob("nightly-*.md"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    pruned += 1
            except OSError:
                pass
        log(f"  pruned {pruned} old report(s)")

        log(f"=== Nightly pass complete (failures={failures}) ===")
        return 1 if failures else 0
    except Exception as exc:
        terminal_error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        try:
            _record_nightly_result(today, failures, terminal_error)
        except Exception as exc:
            print(f"scheduled_nightly: could not record result: {exc}", file=sys.stderr)
        try:
            current = maint_lock.read_text(encoding="utf-8").strip()
            if current == str(os.getpid()):
                maint_lock.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
