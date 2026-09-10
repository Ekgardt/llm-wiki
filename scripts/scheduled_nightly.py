"""Nightly consolidation pass — started at 03:00 by the installed scheduler.

The scheduler is Task Scheduler on Windows, a user LaunchAgent on macOS, a
user systemd timer on Linux, cron as the explicit degraded fallback
(`install_control.py`). The pass runs, in order: capture-intent adoption,
runtime reclaim, the deferred memory queue, yesterday's session
consolidation; the compile (spawned through `maybe_compile`, followed until
it finishes or the wait bound passes) and user-turn keying; then the steps
that read the compile's output — orphaned-checkpoint clearing, structural
lint, backlink repair, the FTS5 index, registered-repository refresh,
generation pruning, model weights, the bounded generation refresh —
telemetry compaction, the health report, report pruning and the bounded
fast-forward of the checkout. Never requires user interaction. All output
goes to $LLM_WIKI_STATE_ROOT/logs/nightly-YYYY-MM-DD.md.
"""

from __future__ import annotations

import inspect
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
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
# The refresh of every registered foreign repository shares one bound; the
# refresh itself is incremental (measured 2026-09-10: 16 s after one edited
# file in a 1 022-file repository, against 60 s for the full build), and a
# repository that does not fit is deferred to the next night, never half-built.
REPOSITORY_REFRESH_BUDGET_SECONDS = 15 * 60


def _generation_result(ownership: OwnerLease | None) -> dict:
    """Pass the owner only when the shared builder still accepts one."""
    arguments = {
        "root": ROOT,
        "state_root": STATE_ROOT,
        "time_budget_seconds": NIGHTLY_GENERATION_BUDGET_SECONDS,
        "max_sources": DEFAULT_GENERATION_SOURCE_LIMIT,
    }
    if ownership is None or not _accepts_ownership(run_generation_maintenance):
        return run_generation_maintenance(**arguments)
    return run_generation_maintenance(**arguments, ownership=ownership)


def _refresh_generation(log, *, ownership: OwnerLease | None = None) -> int:
    """Run the shared bounded builder under its fenced maintenance owner."""
    result = _generation_result(ownership)
    status = result["status"]
    generation = result.get("generation_id") or "none"
    log(
        f"  generation: {status} (id={generation}, "
        f"partial={bool(result.get('partial'))}, reason={result.get('reason') or 'none'})"
    )
    _log_generation_details(log, result)
    return 0 if status in {"built", "current"} else 1


def _log_generation_details(log, result: dict) -> None:
    """A deferred refresh must say what it saw, not only that it stopped.

    A lost maintenance fence carries which check saw it and what the owner row
    held. The nightly log used to drop that on the floor, so the one place where
    the loss actually happens was also the one place with no evidence.
    """
    details = result.get("details")
    if not details:
        return
    log(f"  generation: details {details}")


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
            # The date alone cannot say whether a 03:00 run is late; the health
            # check needs an instant to measure an interval against.
            state["last_nightly_at"] = datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            )
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


def _capture_adoption_step() -> _Step:
    """Dispatch intents published durably but never given a task.

    The capture worker sweeps these too, but it only runs when a capture wakes
    it. If the failure that orphaned an intent is the same one stopping captures
    from finishing, nothing would ever run the sweeper — so recovery cannot
    depend on another capture arriving. This is the pass that does not.
    """
    return _Step(
        "Step 0: adopting undispatched capture intents...",
        "capture_adoption",
        _script("capture_adoption.py"),
        120,
    )


def _reclaim_step() -> _Step:
    """Finish what a hook is too impatient to finish, before anything else runs.

    A hook drains the project-checkpoint queue with a 0.5 s state-lock budget
    because a person is waiting on it; a backlog therefore outlives every hook
    and grows `run/state.json`, which makes the next hook slower still. Nothing
    else in this pass breaks that loop, and every later step reads the state
    file this one shrinks.
    """
    return _Step(
        "Step 0b: reclaiming runtime state...",
        "reclaim",
        _script("reclaim_runtime_state.py"),
        180,
    )


def _queue_step() -> _Step:
    return _Step(
        "Step 1: working deferred memory queue...",
        "work",
        _script("memory_queue.py") + ["work"],
        600,
    )


def _episode_step() -> _Step:
    """Consolidate yesterday's sessions before compile reads the daily log.

    Sessions are kept verbatim whatever the classifier thought of them; this is
    where a day of them becomes durable knowledge, in the window where nobody is
    waiting. Every promoted item must quote the record it came from.
    """
    return _Step(
        "Step 1b: consolidating yesterday's sessions...",
        "episodes",
        _script("episode_consolidation.py"),
        300,
    )


def _compile_step() -> _Step:
    return _Step(
        "Step 2: triggering compile (if needed)...",
        "maybe_compile",
        _script("maybe_compile.py"),
        60,
    )


def _fact_keys_step() -> _Step:
    """Key the user turns of new daily entries, so retrieval can find a fact by its statement.

    One provider call per twenty-five turns, in this window where nobody is
    waiting; a turn is keyed once. See `fact_keys`.
    """
    return _Step(
        "Step 2a: keying new user turns...",
        "fact_keys",
        _script("fact_keys.py"),
        660,
    )


def _checkpoint_step() -> _Step:
    """Clear a checkpoint sequence whose own request is never coming back.

    The design clears a quarantined or reserved sequence the right way: the
    original request arrives again, re-derives the same name, and is given a
    fresh attempt. A session-end checkpoint has no such second arrival — the
    session is over — so a sequence that loses its race stays unsettled and
    blocks every sequence behind it for that project.

    Measured on this vault on 2026-09-07: `llm-wiki` 2214 lost a precondition
    during the benchmark runs and 2215 sat reserved behind it, `no-hands` 830
    likewise. Six hundred hook failures accumulated over a day, one per
    session end, and clearing it took a person running a repair script by
    hand — which is the thing this pass exists to stop needing.

    Safe to run every night: it takes only rows no live lease owns, and does
    nothing on a vault that has none.
    """
    return _Step(
        "Step 3c: clearing checkpoints nothing will settle...",
        "checkpoints",
        _script("repair_orphaned_checkpoint_names.py"),
        120,
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
            "Step 3a: repairing owed backlinks...",
            "backlinks",
            _script("repair_backlinks.py") + ["--apply"],
            120,
        ),
        _Step(
            "Step 3b: rebuilding FTS5 search index...",
            "search",
            _script("search_memory.py") + ["--rebuild"],
            60,
        ),
        _Step(
            # Issue #24, section A: the timer half of the background refresh.
            # Every registered repository whose checkout still exists is looked
            # at and rebuilt incrementally only when its sources changed, each
            # under its own per-repository fence.
            "Step 3c: refreshing registered repository generations...",
            "repositories",
            _script("repository_index.py") + ["refresh-all"],
            REPOSITORY_REFRESH_BUDGET_SECONDS,
        ),
        _Step(
            # Every refresh publishes a new immutable generation and nothing
            # removed the old ones: five in one day, 1.05 GB, on the vault of
            # issue #29. The pruner keeps the active generation and one ancestor.
            "Step 3d: pruning superseded evidence generations...",
            "prune_generations",
            _script("prune_generations.py") + ["--apply"],
            300,
        ),
        _checkpoint_step(),
        _Step(
            # The read path loads weights local-only; a cache that lacks the
            # two pinned models answers by words alone. Present files are not
            # fetched again, so this is a no-op on every night but the first.
            "Step 3f: fetching missing model weights...",
            "models",
            _script("install_models.py"),
            1800,
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


# How long a nightly pass follows a running compile before deferring the
# steps that read its output. Five minutes was the old bound; issue #21
# measured a healthy compile of one daily log at 6.5 minutes through the
# Claude CLI and the pass recorded it as two failures. Thirty minutes is
# the new floor, and an operator sets `MEMORY_COMPILE_WAIT_SECONDS`.
COMPILE_WAIT_SECONDS = 1800.0
COMPILE_WAIT_ENV = "MEMORY_COMPILE_WAIT_SECONDS"


def _compile_wait_seconds() -> float:
    raw = os.environ.get(COMPILE_WAIT_ENV, "").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return COMPILE_WAIT_SECONDS


def _wait_compile_finished() -> bool:
    """Follow a running compile until it stops or the wait bound passes."""
    deadline = time.monotonic() + _compile_wait_seconds()
    while _compile_running():
        if time.monotonic() >= deadline:
            return False
        time.sleep(5)
    return True


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

    # Step 3e: one full health report, read at session start instead of measured there.
    log("Step 3e: writing the health report...")
    _write_health_report(log)
    return failures


# Issue #23.5: session start allows the doctor 0.1 s and said "not measured"
# every morning. The night has the time; the morning reads what it wrote.
HEALTH_REPORT_NAME = "doctor-report.json"
HEALTH_REPORT_BUDGET_SECONDS = 60


def _write_health_report(log) -> None:
    """A full doctor run, written where session start can read it. Never fails the night."""
    from doctor import run_doctor

    try:
        report = run_doctor(
            root=ROOT, state_root=STATE_ROOT, time_budget_seconds=HEALTH_REPORT_BUDGET_SECONDS
        )
        payload = {
            "schema_version": "health-report/v1",
            "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "report": report,
        }
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        (REPORTS_DIR / HEALTH_REPORT_NAME).write_text(
            json.dumps(payload, sort_keys=True, default=str), encoding="utf-8"
        )
        log(f"  health: {report.get('overall_status', 'unknown')}")
    except Exception as exc:  # noqa: BLE001 - a report is never a reason to fail
        log(f"  health report skipped: {type(exc).__name__}")


def _update_code(log) -> None:
    """Advance the checkout last, so changed code takes effect next pass.

    An update is never a reason to fail the night: a diverged branch, an offline
    machine and a file the owner is editing are ordinary states, and the step
    names which one it met.
    """
    from self_update import update_checkout

    log("Step 5: updating the vault code...")
    outcome = update_checkout(ROOT)
    log(f"  update: {outcome['status']} ({outcome.get('reason') or 'none'})")


def _prune_reports(log) -> None:
    """Retention over every maintenance report family and its artifacts."""
    log("Step 4: pruning maintenance reports and artifacts...")
    log(f"  pruned {prune_maintenance_output()} old file(s)")


def _nightly_steps(run_step, log, ownership: OwnerLease | None) -> int:
    failures = _run_steps(
        run_step,
        log,
        [_capture_adoption_step(), _reclaim_step(), _queue_step(), _episode_step()],
    )

    # Step 2 must not skip compile just because a hook-triggered one runs.
    _wait_for_compile_idle(log)
    before = _last_compile_finished()
    failures += _run_steps(run_step, log, [_compile_step(), _fact_keys_step()])

    log("Step 2b: waiting for compile to finish...")
    if not _wait_compile_finished():
        # A compile that is still running is deferred, not failed: its outcome
        # is unknown, and the steps that read its output wait for the next
        # pass. Counting it as a failure turned a slow healthy night red (#21).
        log("WARNING: compile still running past the wait bound — lint/index/graph deferred to the next pass")
        return failures
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
        _update_code(log)

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
