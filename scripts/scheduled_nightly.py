"""Nightly consolidation — runs at 03:00 via Windows Task Scheduler.

What it does:
1. Work any pending queue tasks (deferred LLM work).
2. Force-spawn compile to process all uncompiled daily logs.
3. Run lint and bounded immutable generation maintenance.

Designed to be invoked by Task Scheduler; never requires user interaction.
All output goes to $LLM_WIKI_STATE_ROOT/logs/nightly-YYYY-MM-DD.md.
"""

from __future__ import annotations

import inspect
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import maybe_compile  # noqa: E402
from doctor import (  # noqa: E402
    DEFAULT_GENERATION_SOURCE_LIMIT,
    run_generation_maintenance,
)
from maintenance_helpers import prune_maintenance_output  # noqa: E402
from maintenance_helpers import run_step as _run_step  # noqa: E402
from maintenance_helpers import wait_for_compile_idle as _wait_for_compile_idle
from memory_state import (  # noqa: E402
    REPORTS_DIR,
    ROOT,
    STATE_ROOT,
    load_state,
    update_state,
)
from operational_ownership import (  # noqa: E402
    OwnerLease,
    heartbeat_owner,
)

# How long the nightly pass will spend rebuilding the evidence generation.
# The interactive default is one minute, which is the right bound for a doctor
# run someone is waiting on. A nightly window is not that: on this vault a full
# build of 762 sources takes 98 seconds, so a one-minute bound deferred every
# night and the generation was never rebuilt at all. The unit itself has no
# start timeout, so the only bound that matters is this one.
NIGHTLY_GENERATION_BUDGET_SECONDS = 15 * 60


def _refresh_generation(log, *, ownership: OwnerLease | None = None) -> int:
    """Run the shared bounded builder under its fenced maintenance owner."""
    arguments = {
        "root": ROOT,
        "state_root": STATE_ROOT,
        "time_budget_seconds": NIGHTLY_GENERATION_BUDGET_SECONDS,
        "max_sources": DEFAULT_GENERATION_SOURCE_LIMIT,
    }
    if ownership is not None and _accepts_ownership(run_generation_maintenance):
        result = run_generation_maintenance(**arguments, ownership=ownership)
    else:
        result = run_generation_maintenance(**arguments)
    status = result["status"]
    generation = result.get("generation_id") or "none"
    log(
        f"  generation: {status} (id={generation}, "
        f"partial={bool(result.get('partial'))}, reason={result.get('reason') or 'none'})"
    )
    return 0 if status in {"built", "current"} else 1


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


def _accepts_ownership(function) -> bool:
    parameters = inspect.signature(function).parameters
    return "ownership" in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


@dataclass(frozen=True)
class _Step:
    """One nightly subprocess step: what to announce, run, and how long to wait."""

    message: str
    label: str
    command: list[str]
    timeout: int


def _script(name: str) -> list[str]:
    return [sys.executable, str(ROOT / "scripts" / name)]


def _queue_step() -> _Step:
    return _Step(
        "Step 1: working deferred memory queue...",
        "work",
        _script("memory_queue.py") + ["work"],
        600,
    )


def _compile_step() -> _Step:
    return _Step(
        "Step 2: triggering compile (if needed)...",
        "maybe_compile",
        _script("maybe_compile.py"),
        60,
    )


def _post_compile_steps() -> list[_Step]:
    return [
        _Step(
            "Step 3: structural lint...",
            "lint",
            _script("lint_memory.py"),
            120,
        ),
        _Step(
            "Step 3b: rebuilding FTS5 search index...",
            "search",
            _script("search_memory.py") + ["--rebuild"],
            60,
        ),
    ]


def _run_steps(run_step, log, steps: list[_Step]) -> int:
    """Run each step in order and count the ones that failed."""
    failures = 0
    for step in steps:
        log(step.message)
        failures += int(bool(run_step(step.command, log, step.label, timeout=step.timeout)))
    return failures


def _compile_running() -> bool:
    """Best effort: an unreadable status counts as finished, as before."""
    try:
        return bool(maybe_compile.status()["compile_running"])
    except Exception:  # noqa: BLE001
        return False


def _safe_state() -> dict:
    """State is a report here, never a precondition; unreadable means unknown."""
    try:
        return load_state()
    except Exception:  # noqa: BLE001 - a nightly pass never fails on diagnostics
        return {}


def _compile_failed_this_pass(before: str | None) -> str | None:
    """The error of a compile that ran in this pass, or None.

    `maybe_compile` spawns the compile and returns 0 as soon as it is running,
    so the step it belongs to says nothing about the outcome. Waiting for the
    process to stop says nothing either. On 2026-08-22 that let a nightly pass
    report `failures=0` for a night whose compile had died a second in. The
    stamp comparison keeps last night's error out of tonight's count.
    """
    state = _safe_state()
    finished = state.get("last_compile_finished_at")
    if not finished or str(finished) == before:
        return None
    if state.get("last_compile_status") != "error":
        return None
    return str(state.get("last_compile_error") or "unknown")


def _last_compile_finished() -> str | None:
    finished = _safe_state().get("last_compile_finished_at")
    return str(finished) if finished else None


def _wait_compile_finished() -> bool:
    """Wait up to 5 minutes (60 × 5s) for a running compile to finish."""
    for _ in range(60):
        if not _compile_running():
            return True
        time.sleep(5)
    return False


def _compact_telemetry(log) -> None:
    """Compact disposable telemetry without touching knowledge."""
    try:
        from retrieval_telemetry import compact

        log(f"  telemetry: compacted {compact()} event(s)")
    except Exception as e:  # noqa: BLE001
        log(f"  telemetry: failed ({e}) — skipping")


def _post_compile_pass(run_step, log, ownership: OwnerLease | None) -> int:
    failures = _run_steps(run_step, log, _post_compile_steps())

    # Step 3c: refresh one immutable generation under the shared fence.
    log("Step 3c: refreshing immutable evidence generation...")
    failures += _refresh_generation(log, ownership=ownership)

    # Step 3d: compact disposable telemetry without touching knowledge.
    log("Step 3d: compacting retrieval telemetry...")
    _compact_telemetry(log)
    return failures


def _prune_reports(log) -> None:
    """Retention over every maintenance report family and its artifacts."""
    log("Step 4: pruning maintenance reports and artifacts...")
    log(f"  pruned {prune_maintenance_output()} old file(s)")


def _nightly_steps(run_step, log, ownership: OwnerLease | None) -> int:
    failures = _run_steps(run_step, log, [_queue_step()])

    # Step 2 must not skip compile just because a hook-triggered one runs.
    _wait_for_compile_idle(log)
    before = _last_compile_finished()
    failures += _run_steps(run_step, log, [_compile_step()])

    log("Step 2b: waiting for compile to finish...")
    if not _wait_compile_finished():
        log("WARNING: compile still running after 5 min — skipping lint/index/graph")
        # Steps 3, 3b, 3c depend on compile output.
        return failures + 1
    failures += _report_compile_outcome(log, before)
    return failures + _post_compile_pass(run_step, log, ownership)


def _report_compile_outcome(log, before: str | None) -> int:
    error = _compile_failed_this_pass(before)
    if error is None:
        return 0
    log(f"  compile: FAILED — {error}")
    return 1


def _require_nightly_owner(ownership: OwnerLease | None) -> None:
    if ownership is None:
        return
    if ownership.role in {"nightly", "weekly"} and ownership.scope == "global":
        return
    raise ValueError("nightly work requires a nightly or weekly global owner")


def _nightly_logger(log_file: Path):
    def log(msg: str) -> None:
        line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
        print(line)
        with log_file.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    return log


def _owned_step_runner(ownership: OwnerLease | None):
    """Pass the maintenance owner through to steps that accept one."""
    def run_step(command, log, name, *, timeout):
        if ownership is not None and _accepts_ownership(_run_step):
            return _run_step(command, log, name, timeout=timeout, ownership=ownership)
        return _run_step(command, log, name, timeout=timeout)

    return run_step


def _run_nightly_body(*, ownership: OwnerLease | None) -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    _require_nightly_owner(ownership)
    run_step = _owned_step_runner(ownership)

    failures = 1
    terminal_error = None
    try:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        log = _nightly_logger(REPORTS_DIR / f"nightly-{today}.md")
        log(f"=== Nightly consolidation pass — {today} ===")

        failures = _nightly_steps(run_step, log, ownership)
        _prune_reports(log)

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


def run_nightly(*, ownership: OwnerLease | None) -> int:
    if ownership is None or ownership.role == "weekly":
        return _run_nightly_body(ownership=ownership)
    with heartbeat_owner(ownership):
        return _run_nightly_body(ownership=ownership)


def _write_marker(marker: Path) -> bool:
    """Create the marker exclusively and stamp it with this PID."""
    try:
        descriptor = os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    try:
        os.write(descriptor, str(os.getpid()).encode("ascii"))
    finally:
        os.close(descriptor)
    return True


def _marker_is_abandoned(marker: Path) -> bool:
    """A marker older than 30 minutes whose owner process is gone."""
    from memory_state import _is_pid_alive

    try:
        if time.time() - marker.stat().st_mtime <= 1800:
            return False
        old_pid = int(marker.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return not _is_pid_alive(old_pid)


def _steal_marker(marker: Path) -> bool:
    if not _marker_is_abandoned(marker):
        return False
    try:
        marker.unlink()
    except OSError:
        return False
    return _write_marker(marker)


def _acquire_legacy_maintenance_marker() -> Path | None:
    marker = STATE_ROOT / "run/maintenance.lock"
    marker.parent.mkdir(parents=True, exist_ok=True)
    if _write_marker(marker):
        return marker
    return marker if _steal_marker(marker) else None


def _release_legacy_maintenance_marker(marker: Path) -> None:
    try:
        if marker.read_text(encoding="utf-8").strip() == str(os.getpid()):
            marker.unlink()
    except OSError:
        pass


def main() -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    marker = _acquire_legacy_maintenance_marker()
    if marker is None:
        print("scheduled_nightly: maintenance already running, skipping.", file=sys.stderr)
        try:
            _record_nightly_skip(today, "maintenance_lock_held")
        except Exception as exc:
            print(f"scheduled_nightly: could not record skip: {exc}", file=sys.stderr)
        return 0
    try:
        return run_nightly(ownership=None)
    finally:
        _release_legacy_maintenance_marker(marker)


if __name__ == "__main__":
    raise SystemExit(main())
