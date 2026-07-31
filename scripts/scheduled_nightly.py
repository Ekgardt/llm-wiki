"""Nightly consolidation — runs at 03:00 via Windows Task Scheduler.

What it does:
1. Drain any pending queue tasks (deferred LLM work).
2. Force-spawn compile to process all uncompiled daily logs.
3. Run lint and append to a rolling log file.

Designed to be invoked by Task Scheduler; never requires user interaction.
All output goes to $LLM_WIKI_STATE_ROOT/logs/nightly-YYYY-MM-DD.md.
"""

from __future__ import annotations

import os
import sys
import time
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import maybe_compile  # noqa: E402
from maintenance_helpers import run_step as _run_step  # noqa: E402
from maintenance_helpers import wait_for_compile_idle as _wait_for_compile_idle
from maintenance_helpers import write_console as _write_console
from memory_queue import status as queue_status  # noqa: E402
from memory_state import (  # noqa: E402
    REPORTS_DIR,
    ROOT,
    STATE_ROOT,
    advisory_file_lock,
    load_state,
)


def _rebuild_derived_indexes(log, *, include_markdown: bool = False) -> int:
    """Rebuild derived navigation/search artifacts and return failure count."""
    failures = 0
    if include_markdown:
        log("Rebuilding Markdown knowledge index...")
        failures += bool(
            _run_step(
                [sys.executable, str(ROOT / "scripts" / "rebuild_memory_index.py")],
                log,
                "markdown-index",
                timeout=60,
            )
        )

    log("Rebuilding FTS5 search index...")
    failures += bool(
        _run_step(
            [sys.executable, str(ROOT / "scripts" / "search_memory.py"), "--rebuild"],
            log,
            "search",
            timeout=60,
        )
    )

    log("Rebuilding wikilink graph cache...")
    scripts_path = str(ROOT / "scripts")
    env_code = (
        f"import sys; sys.path.insert(0, {scripts_path!r}); "
        "from graph_neighbors import rebuild_graph_cache; "
        "print(rebuild_graph_cache())"
    )
    failures += bool(
        _run_step(
            [sys.executable, "-c", env_code],
            log,
            "graph",
            timeout=60,
        )
    )
    return failures


def _wait_for_compile_finish(
    log, before: dict, *, detached_expected: bool
) -> tuple[dict, dict, bool]:
    """Wait for detached compile startup/completion and return final snapshots."""
    before_started = before.get("last_compile_started_at")
    for _ in range(60):  # max 5 minutes (60 x 5s)
        compile_status = maybe_compile.status()
        state = load_state()
        state_status = state.get("last_compile_status")
        started_changed = state.get("last_compile_started_at") != before_started
        if compile_status["compile_running"] or state_status == "running":
            time.sleep(5)
            continue
        if not detached_expected or not compile_status.get("pending_work"):
            return compile_status, state, False
        if started_changed:
            return compile_status, state, False
        time.sleep(5)
    return maybe_compile.status(), load_state(), True


def main(*, acquire_lease: bool = True, rebuild_indexes: bool = True) -> int:
    maint_lock = STATE_ROOT / "run" / "maintenance.lock"
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = REPORTS_DIR / f"nightly-{today}.md"
    try:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _write_console(
            f"scheduled_nightly: ERROR: report setup failed ({type(exc).__name__}: {exc})",
            error=True,
        )
        return 1

    def log(msg: str) -> None:
        line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
        _write_console(line)
        with log_file.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    locks = ExitStack()
    if acquire_lease:
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
        log(f"=== Nightly consolidation pass — {today} ===")
        failures = 0

        sdk_deferred = os.environ.get("MEMORY_LLM_PROVIDER", "").lower().strip() == "opencode-sdk"

        # SDK work needs an authenticated active OpenCode session. Leave it
        # pending while still running all deterministic maintenance below.
        if sdk_deferred:
            log("Step 1: deferred queue awaits the next OpenCode SDK session.")
        else:
            log("Step 1: draining deferred memory queue...")
            rc = _run_step(
                [sys.executable, str(ROOT / "scripts" / "memory_queue.py"), "drain"],
                log,
                "drain",
                timeout=600,
            )
            if rc:
                failures += 1

        # Step 2: maybe_compile (will spawn compile if there's pending work).
        _wait_for_compile_idle(log)
        before_compile = load_state()
        log("Step 2: triggering compile (if needed)...")
        rc = _run_step(
            [sys.executable, str(ROOT / "scripts" / "maybe_compile.py")],
            log,
            "maybe_compile",
            timeout=60,
        )
        if rc:
            failures += 1

        # Step 2b: wait for detached startup and completion before inspecting state.
        log("Step 2b: waiting for compile to finish...")
        compile_status, compile_state, compile_still_running = _wait_for_compile_finish(
            log,
            before_compile,
            detached_expected=not sdk_deferred and rc == 0,
        )

        if compile_still_running:
            log("ERROR: compile did not reach a final state after 5 min; skipping lint/index/graph")
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

            if rebuild_indexes:
                log("Step 3b/3c: rebuilding search and graph indexes...")
                failures += _rebuild_derived_indexes(log)
            else:
                log("Step 3b/3c: derived rebuild deferred until weekly mutations finish.")

        # A detached launcher returning zero does not prove the compile succeeded.
        # Re-read both durable state and queue state after all SDK/detached behavior.
        compile_status = maybe_compile.status()
        compile_state = load_state()
        final_compile_status = str(compile_state.get("last_compile_status") or "unknown")
        compile_error = str(compile_state.get("last_compile_error") or "").strip()
        state_keys = (
            "last_compile_started_at",
            "last_compile_finished_at",
            "last_compile_status",
            "last_compile_error",
        )
        compile_state_changed = any(
            before_compile.get(key) != compile_state.get(key) for key in state_keys
        )
        compile_failed = compile_state_changed and final_compile_status in {
            "error",
            "warning",
            "running",
        }
        if compile_failed:
            detail = f": {compile_error}" if compile_error else ""
            log(f"ERROR: final compile status: {final_compile_status}{detail}")
        if compile_status.get("compile_running"):
            log(f"ERROR: compile remains active ({compile_status.get('reason', 'unknown')})")
            compile_failed = True
        if compile_status.get("pending_work"):
            log("ERROR: pending compile work remains after maintenance.")
            compile_failed = True
        if compile_failed and not compile_still_running:
            failures += 1

        queue = queue_status()
        pending_total = int(queue.get("pending_total", 0))
        in_flight = int(queue.get("in_flight", 0))
        outstanding_total = int(
            queue.get("outstanding_total", pending_total + in_flight)
        )
        if outstanding_total:
            type_counts: dict[str, int] = {}
            for source in (queue.get("by_type", {}), queue.get("in_flight_by_type", {})):
                for kind, count in source.items():
                    type_counts[kind] = type_counts.get(kind, 0) + int(count)
            by_type = (
                ", ".join(
                    f"{kind}={count}" for kind, count in sorted(type_counts.items())
                )
                or "types unknown"
            )
            if in_flight:
                log(
                    f"ERROR: queue still outstanding: {outstanding_total} task(s) "
                    f"(pending={pending_total}, in_flight={in_flight}; {by_type})."
                )
            else:
                log(
                    f"ERROR: queue still pending: {pending_total} task(s) "
                    f"({by_type})."
                )
            failures += 1

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


if __name__ == "__main__":
    raise SystemExit(main())
