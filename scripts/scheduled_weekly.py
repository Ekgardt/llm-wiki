"""Weekly deep maintenance — started Sunday 04:00 by the installed scheduler.

The scheduler is the same one that starts the nightly pass (`install_control.py`).
The nightly ran at 03:00, so the weekly does not repeat it; it runs only its own
steps, in order: the OKF conformance sweep, a queue status report, the queue
purge (finished work past its retention, exported to the private raw archive
first), stale-page archiving, session-record archiving, daily-log archiving past
the hot window,
superseded-generation pruning, the opt-in contradiction check, A-MEM reflection
and the L1 tier overviews.

It keeps its own terminal record in `run/state.json` (`last_weekly_status`,
`last_weekly_at`, `last_weekly_failure`), which doctor's scheduler check reads;
it never writes the nightly's. See
`docs/research/2026-09-24-the-weekly-pass-has-its-own-record.md`.

Designed to run unattended. Logs to $LLM_WIKI_STATE_ROOT/logs/weekly-YYYY-MM-DD.md.
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scheduled_nightly  # noqa: E402
from llm_wiki import runtime_roots
from maintenance_helpers import wait_for_compile_idle as _wait_for_compile_idle
from memory_state import update_state  # noqa: E402
from operational_ownership import (  # noqa: E402
    OwnerLease,
    heartbeat_owner,
)
from scheduled_nightly import StepLog  # noqa: E402
from weekly_budget import (
    CONTRADICTIONS_STEP_SECONDS,
    FENCE_POLL_SECONDS,
    REFLECTION_BUDGET_SECONDS,
    fence_wait_seconds,
    script_steps,
)


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


def _step_runner(fence: threading.Event | None):
    """Run one subprocess step; a lost fence stops the pass before the next."""
    return scheduled_nightly.fenced_step_runner(fence)


def _logger(log_file: Path):
    def log(msg: str) -> None:
        line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
        print(line)
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    return log


def _run_script_steps(run_step, log: StepLog) -> int:
    failures = 0
    for message, label, command, timeout in script_steps():
        log.step(message)
        failures += int(bool(run_step(command, log, label, timeout=timeout)))
    return failures


def _run_contradictions(run_step, log: StepLog) -> int:
    if not _contradictions_wanted():
        log.step("contradiction check SKIPPED (set MEMORY_WEEKLY_CONTRADICTIONS=1 to enable)")
        return 0
    log.step("LLM contradiction check (opt-in)...")
    command = [sys.executable, str(runtime_roots.vault_root() / "scripts" / "lint_memory.py"), "--contradictions"]
    return int(bool(run_step(command, log, "contradictions", timeout=CONTRADICTIONS_STEP_SECONDS)))


def _reflect(log: StepLog) -> int:
    """A-MEM reflection — consolidate pages; a failure is counted, not only logged.

    It and the tier step logged their exceptions and the pass still reported
    success (audit C-32,
    docs/research/2026-09-25-a-weekly-pass-counts-what-failed.md).
    """
    log.step("A-MEM reflection (page consolidation)...")
    try:
        reflect_candidates(log)
    except Exception as error:  # noqa: BLE001 - one step, counted below
        log(f"  reflection: failed ({error})")
        return 1
    return 0


def reflect_candidates(log) -> None:
    from reflection import find_reflection_candidates, reflect_page

    candidates = find_reflection_candidates()
    if not candidates:
        log("  No reflection candidates found")
        return
    log(f"  Found {len(candidates)} reflection candidate(s)")
    deadline = time.monotonic() + REFLECTION_BUDGET_SECONDS
    for done, candidate in enumerate(candidates):
        if time.monotonic() >= deadline:
            log(f"  reflection budget spent: {len(candidates) - done} page(s) wait for next week")
            return
        log(f"  {reflect_page(candidate['path'], apply=True)}")


def fence_unless_busy():
    """The weekly fence, or None while another pass holds it; any other refusal raises."""
    from operational_ownership import OperationalOwnershipError

    try:
        return scheduled_nightly.take_scheduled_fence("weekly")
    except OperationalOwnershipError as exc:
        if exc.code != "owner_busy":
            raise
        return None


def fence_after_waiting(sleep=time.sleep, clock=time.monotonic):
    """Wait out a nightly that holds the fence; None only once it outlasted its own bounds."""
    deadline = clock() + fence_wait_seconds()
    while True:
        fence = fence_unless_busy()
        if fence is not None or clock() >= deadline:
            return fence
        sleep(FENCE_POLL_SECONDS)


def build_tiers(log: StepLog) -> int:
    """Generate L1 tier overviews; a failure is counted, not only logged."""
    log.step("generating L1 tier overviews...")
    try:
        from build_tiers import build_all_tiers

        stats = build_all_tiers(use_llm=False, verbose=False)
    except Exception as error:  # noqa: BLE001 - one step, counted below
        log(f"  tiers: failed ({error})")
        return 1
    log(f"  tiers: {stats['generated']} generated, {stats['skipped']} skipped")
    return 0


def record_weekly_result(failures: int, error: str | None = None) -> None:
    """The weekly's own terminal record; a failure never overwrites the nightly's."""
    now = datetime.now(UTC).isoformat(timespec="seconds")

    def _mutate(state: dict) -> None:
        if not failures:
            state["last_weekly_status"] = "success"
            state["last_weekly_at"] = now
            state.pop("last_weekly_failure", None)
            return
        state["last_weekly_status"] = "failed"
        state["last_weekly_failure"] = {
            "failed_at": now,
            "failures": failures,
            **({"error": error} if error else {}),
        }

    update_state(_mutate)


def record_weekly_start() -> None:
    """A weekly that starts says so: one killed before its record is then visible
    (audit 2026-09-27 B-15, docs/research/2026-09-27-a-weekly-that-never-finishes-is-seen.md)."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    update_state(lambda state: state.__setitem__("last_weekly_started_at", now))


def record_weekly_skip(reason: str) -> None:
    """A weekly that did not run says so, so a stale weekly names why."""
    now = datetime.now(UTC).isoformat(timespec="seconds")

    def _mutate(state: dict) -> None:
        state["last_weekly_skip"] = {"skipped_at": now, "reason": reason}

    update_state(_mutate)


def _skipped(reason: str) -> int:
    print(f"scheduled_weekly: maintenance already running ({reason}), skipping.", file=sys.stderr)
    try:
        record_weekly_skip(reason)
    except Exception as exc:  # noqa: BLE001 - the skip itself is the outcome
        print(f"scheduled_weekly: could not record the skip: {exc}", file=sys.stderr)
    return 0


def _record_quietly(failures: int, error: str | None = None) -> None:
    try:
        record_weekly_result(failures, error)
    except Exception as exc:  # noqa: BLE001 - the pass's own outcome is what matters
        print(f"scheduled_weekly: could not record result: {exc}", file=sys.stderr)


def _run_weekly_steps(run_step, log: StepLog) -> int:
    _wait_for_compile_idle(log)
    failures = _run_script_steps(run_step, log)
    failures += _run_contradictions(run_step, log)
    failures += _reflect(log)
    failures += build_tiers(log)
    return failures


def _run_weekly_body(
    *, ownership: OwnerLease | None, fence: threading.Event | None = None
) -> int:
    _require_weekly_owner(ownership)
    run_step = _step_runner(fence)
    today = datetime.now().strftime("%Y-%m-%d")
    runtime_roots.reports_dir().mkdir(parents=True, exist_ok=True)
    log = StepLog(_logger(runtime_roots.reports_dir() / f"weekly-{today}.md"))

    log(f"=== Weekly deep maintenance — {today} ===")
    failures = 1
    try:
        failures = _run_weekly_steps(run_step, log)
    finally:
        _record_quietly(failures)
    log(f"=== Weekly deep maintenance complete (failures={failures}) ===")
    return 1 if failures else 0


def run_weekly(
    *, ownership: OwnerLease | None, registry: object | None = None
) -> int:
    if ownership is None:
        return _run_weekly_body(ownership=None)
    lost = threading.Event()
    with heartbeat_owner(ownership, registry=registry, lost=lost):
        return _run_weekly_body(ownership=ownership, fence=lost)


def main(argv: list[str] | tuple[str, ...] = ()) -> int:
    """The weekly fence: canonical on an adopted vault, the legacy marker otherwise."""
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    from operational_ownership import OperationalOwnershipError
    from secret_redact import describe_error_chain

    try:
        fence = fence_after_waiting()
    except OperationalOwnershipError as exc:
        return _skipped(str(exc.code))
    except Exception as exc:
        _record_quietly(1, describe_error_chain(exc))
        raise
    if fence is None:
        return _skipped("fence held")
    try:
        record_weekly_start()
        return run_weekly(ownership=fence.lease, registry=fence.registry)
    finally:
        fence.release()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
