"""Nightly consolidation pass — started at 03:00 by the installed scheduler.

The scheduler is Task Scheduler on Windows, a user LaunchAgent on macOS, a
user systemd timer on Linux, cron as the explicit degraded fallback
(`install_control.py`). The pass runs, in order: capture-intent adoption,
runtime reclaim, the deferred memory queue, pending session consolidation;
user-turn keying and the compile (an owned child whose exit is observed directly); then the steps that read the
compile's output — structural lint, backlink repair, registered-repository
refresh and retention, generation pruning, orphaned-checkpoint clearing, LSP
failure-evidence retirement, retirement of the memory's own call transcripts,
retirement of old benchmark runs, model weights, the bounded memory-generation refresh, telemetry compaction
and the health report; last, report pruning and the bounded fast-forward of
the checkout. The log numbers the steps in the order they ran (`StepLog`).
Never requires user interaction. All output goes to
$LLM_WIKI_STATE_ROOT/logs/nightly-YYYY-MM-DD.md.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import python_series  # noqa: E402

python_series.require_supported_python(Path(__file__).resolve().parent.parent)

from datetime import UTC, datetime  # noqa: E402

import compile_lock  # noqa: E402
import nightly_budget  # noqa: E402
import process_liveness  # noqa: E402
from generation_maintenance import (  # noqa: E402
    run_generation_maintenance,
)
from llm_wiki import runtime_roots  # noqa: E402
from llm_wiki.runtime_publication import write_all  # noqa: E402
from maintenance_helpers import prune_maintenance_output, trim_scheduler_logs  # noqa: E402
from maintenance_helpers import run_step as _run_step  # noqa: E402
from maintenance_helpers import wait_for_compile_idle as _wait_for_compile_idle  # noqa: E402
from markdown_transaction import active_or_legacy_coordinator  # noqa: E402
from memory_state import load_state, update_state  # noqa: E402
from operational_ownership import (  # noqa: E402
    OperationalOwnershipError,
    OwnerLease,
    OwnershipRegistry,
    acquire_scheduled_owner,
    heartbeat_owner,
    release_marker_owner,
)
from secret_redact import describe_error  # noqa: E402
from settings import setting_value  # noqa: E402


def generation_result() -> dict:
    """The shared builder takes its own `repair` owner; the pass passes none."""
    return run_generation_maintenance(
        root=runtime_roots.vault_root(),
        state_root=runtime_roots.state_root(),
        time_budget_seconds=setting_value("generation.nightly_seconds", runtime_roots.vault_root()),
        max_sources=setting_value("corpus.max_files", runtime_roots.vault_root()),
    )


def refresh_generation(log) -> int:
    """Run the shared bounded builder under its fenced maintenance owner."""
    result = generation_result()
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


def utc_now() -> str:
    """Every instant in the state is UTC with its offset; a date stays the local calendar day.

    `skipped_at` and `failed_at` were local and `last_nightly_at` UTC, and doctor
    compared them as strings (audit C-30,
    docs/research/2026-09-25-the-scheduler-state-keeps-one-clock.md).
    """
    return datetime.now(UTC).isoformat(timespec="seconds")


def record_nightly_result(today: str, failures: int, error: str | None = None, *, deferred=()) -> None:
    """Release the claim; only a complete pass advances the success timestamp."""
    timestamp = utc_now()

    def mutate(state):
        _release_nightly_claim(state, today)
        status = _nightly_completion_status(failures, deferred)
        state["last_nightly_status"] = status
        state["last_nightly_result"] = {"date": today, "at": timestamp, "status": status,
                                        "failures": failures, "deferred": list(deferred), "error": error}
        _record_completion_fields(state, today, timestamp, status, failures, error)

    update_state(mutate)


def _release_nightly_claim(state, today):
    if state.get("nightly_catchup_claim", {}).get("date") == today:
        state.pop("nightly_catchup_claim", None)


def _nightly_completion_status(failures, deferred):
    if failures:
        return "failed"
    if deferred:
        return "deferred"
    return "success"


def _record_completion_fields(state, today, timestamp, status, failures, error):
    if status == "failed":
        state["last_nightly_failure"] = {"date": today, "failed_at": timestamp,
                                         "failures": failures, **({"error": error} if error else {})}
        return
    if status == "success":
        _record_nightly_success(state, today, timestamp)


def _record_nightly_success(state, today, timestamp):
    state.update(last_nightly_date=today, last_nightly_at=timestamp)
    state.setdefault("weekly_due_since", timestamp)
    state.pop("last_nightly_failure", None)
    state.pop("nightly_deferred_compile", None)


def _record_nightly_skip(today: str, reason: str) -> None:
    """Release today's claim without replacing the last execution result."""
    timestamp = utc_now()

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


class StepLog:
    """A log that numbers each step it announces, in the order the steps run.

    The labels used to be written by hand beside each step and drifted: on
    2026-09-24 "Step 3c" named three different steps. See
    `docs/research/2026-09-24-the-weekly-pass-has-its-own-record.md`.
    """

    def __init__(self, log: Callable[[str], None]) -> None:
        self._log = log
        self._count = 0
        self.deferred: list[str] = []

    def __call__(self, message: str) -> None:
        self._log(message)

    def step(self, message: str) -> None:
        self._count += 1
        self._log(f"Step {self._count}: {message}")


@dataclass(frozen=True)
class _PassResult:
    failures: int
    deferred: tuple[str, ...] = ()


def run_steps(run_step, log, steps: list[nightly_budget.Step]) -> int:
    """Run each step in order and count the ones that failed."""
    failures = 0
    for step in steps:
        log.step(step.message)
        failures += int(bool(run_step(step.command, log, step.label, timeout=step.timeout)))
    return failures


def compile_running() -> bool:
    """Unreadable ownership cannot establish that a compiler finished."""
    return bool(compile_lock.process_status()["compile_running"])


def compact_telemetry(log) -> None:
    """Compact disposable telemetry without touching knowledge."""
    try:
        from retrieval_telemetry import compact

        log(f"  telemetry: compacted {compact()} event(s)")
    except Exception as e:  # noqa: BLE001
        log(f"  telemetry: failed ({e}) — skipping")


def post_compile_pass(run_step, log) -> int:
    failures = run_steps(run_step, log, nightly_budget.post_compile_steps())

    # refresh one immutable generation under the shared fence.
    log.step("refreshing immutable evidence generation...")
    failures += refresh_generation(log)

    # compact disposable telemetry without touching knowledge.
    log.step("compacting retrieval telemetry...")
    compact_telemetry(log)

    # Full diagnosis supplements the current operational check at session start.
    log.step("writing the health report...")
    health = write_health_report(log)
    if health == "deferred":
        log.deferred.append("full_health_incomplete")
    return failures + int(health == "failed")


# Issue #23.5: session start allows the doctor 0.1 s and said "not measured"
# every morning. The night has the time; the morning reads what it wrote.
HEALTH_REPORT_NAME = "doctor-report.json"
def write_health_report(log) -> str:
    """Persist full diagnosis and report whether its requested checks finished."""
    from doctor import run_doctor

    try:
        report = run_doctor(
            root=runtime_roots.vault_root(), state_root=runtime_roots.state_root(), time_budget_seconds=setting_value("doctor.nightly_seconds", runtime_roots.vault_root()), scope="full"
        )
        payload = {
            "schema_version": "health-report/v1",
            "written_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "report": report,
        }
        runtime_roots.reports_dir().mkdir(parents=True, exist_ok=True)
        (runtime_roots.reports_dir() / HEALTH_REPORT_NAME).write_text(
            json.dumps(payload, sort_keys=True, default=str), encoding="utf-8"
        )
        log(f"  health: {report.get('overall_status', 'unknown')}")
        return health_report_outcome(report)
    except Exception as exc:  # noqa: BLE001 - report failure is counted by the pass
        log(f"  health report failed: {describe_error(exc)}")
        return "failed"


def health_report_outcome(report):
    checks = report["checks"]
    if any(check.get("details", {}).get("budget_exhausted") for check in checks):
        return "deferred"
    if any(_health_check_failed(check.get("details", {})) for check in checks):
        return "failed"
    return "complete"


def _health_check_failed(details):
    return bool(details.get("exception") or details.get("read_error"))


def update_code(log) -> None:
    """Advance the checkout last, so changed code takes effect next pass.

    An update is never a reason to fail the night: a diverged branch, an offline
    machine and a file the owner is editing are ordinary states, and the step
    names which one it met.
    """
    from self_update import update_checkout

    log.step("updating the vault code...")
    previous = load_state().get("last_update")
    outcome = carried_forward(update_checkout(runtime_roots.vault_root()), previous)
    update_state(lambda state: state.__setitem__("last_update", update_record(outcome)))
    log(f"  update: {outcome['status']} ({outcome.get('reason') or 'none'})")
    for said in filter(None, (outcome.get("detail"), _required_python_line(outcome))):
        log(f"  update: {said}")
    log_update_aftermath(log, outcome)


def _required_python_line(outcome: dict) -> str | None:
    if not outcome.get("required"):
        return None
    return f"the next commit requires Python {outcome['required']}"


UPDATE_RECORD_FIELDS = ("status", "reason", "required", "dependencies", "resources", "resources_since")


def carried_forward(outcome: dict, previous: object) -> dict:
    """What an earlier update left undone stays named until it is done.

    A night with nothing to fetch overwrote the record, and "dependencies were
    not synced" or "rerun the installer" vanished while still true (audit
    2026-09-26 B-24, docs/research/2026-09-26-an-unfinished-update-stays-named.md).
    Stale dependencies are synced again here; a needed installer run is kept until
    the installer has run since.
    """
    if outcome.get("status") != "current" or not isinstance(previous, dict):
        return outcome
    return {**outcome, **_pending_dependencies(previous), **_pending_resources(previous)}


def _pending_dependencies(previous: dict) -> dict:
    from self_update import sync_dependencies

    if previous.get("dependencies") != "stale":
        return {}
    return {"dependencies": sync_dependencies(runtime_roots.vault_root())}


def _pending_resources(previous: dict) -> dict:
    since = previous.get("resources_since") or previous.get("at")
    if previous.get("resources") != "rerun_installer" or _installed_since(since):
        return {}
    return {"resources": "rerun_installer", "resources_since": since}


def _installed_since(moment: object) -> bool:
    """Whether the installer committed after `moment`; doubt keeps the warning."""
    try:
        manifest = json.loads((runtime_roots.state_root() / "run" / "install" / "manifest.json").read_text(encoding="utf-8"))
        installed = datetime.fromisoformat(str(manifest["committed_at"]))
        return installed > datetime.fromisoformat(str(moment))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def update_record(outcome: dict) -> dict:
    """What the code update did, kept for doctor; before, only the night's log knew.

    See `docs/research/2026-09-25-the-scheduler-says-what-the-night-could-not-do.md`.
    """
    record = {field: outcome.get(field) for field in UPDATE_RECORD_FIELDS}
    record["at"] = datetime.now(UTC).isoformat(timespec="seconds")
    return record


def log_update_aftermath(log, outcome: dict) -> None:
    """What the update brought in but did not bring into force.

    See `docs/research/2026-09-17-an-update-says-what-it-did-not-bring-into-force.md`.
    """
    if outcome.get("status") != "updated":
        return
    extras = ", ".join(outcome.get("extras") or ()) or "none"
    log(f"  update: dependencies {outcome.get('dependencies')} with extras: {extras}")
    log(f"  update: owned resources {outcome.get('resources')}")


def prune_reports(log) -> None:
    """Retention over every maintenance report family and its artifacts."""
    log.step("pruning maintenance reports and artifacts...")
    log(f"  pruned {prune_maintenance_output()} old file(s)")
    log(f"  trimmed {trim_scheduler_logs()} byte(s) from the scheduler logs")


def nightly_steps(run_step, log, _ownership: OwnerLease | None = None) -> int:
    failures = run_steps(run_step, log, nightly_budget.intake_steps())
    _wait_for_compile_idle(log)
    if _defer_entity_writes_for_running_compile(log):
        return failures
    failures += run_steps(run_step, log, [nightly_budget.fact_keys_step()])
    compile_failures = run_steps(run_step, log, [nightly_budget.compile_step()])
    if compile_failures:
        return failures + compile_failures
    return failures + post_compile_pass(run_step, log)


def _defer_entity_writes_for_running_compile(log) -> bool:
    if not compile_running():
        return False
    log("WARNING: compile still running after idle wait — entity writes and compile deferred to the next pass")
    log("  compile outcome is unknown; the idle wait does not establish that its owner finished")
    log.deferred.append("compile_busy")
    return True


def _require_nightly_owner(ownership: OwnerLease | None) -> None:
    if ownership is None:
        return
    if ownership.role == "nightly" and ownership.scope == "global":
        return
    raise ValueError("nightly work requires a nightly global owner")


def _nightly_logger(log_file: Path):
    def log(msg: str) -> None:
        line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
        print(line)
        with log_file.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    return log


def _require_fence(fence: threading.Event | None) -> None:
    """A lost fence stops the pass before the next step, while the loss is fresh."""
    if fence is not None and fence.is_set():
        raise OperationalOwnershipError("owner_fence_lost")


class PassBoundExceeded(RuntimeError):
    """The pass has outlived the worst case it computes for itself."""


def _require_within_bound(deadline: float | None) -> None:
    """A pass that has outlived its own bound stops at the next boundary.

    Windows Task Scheduler is the only scheduler that limits the pass from
    outside; launchd and cron limit nothing, so this is the bound there.
    Research: docs/research/2026-09-18-a-pass-that-knows-how-long-it-can-be.md
    """
    if deadline is not None and time.monotonic() >= deadline:
        raise PassBoundExceeded("the pass outlived its own worst case")


def fenced_step_runner(fence: threading.Event | None, deadline: float | None = None):
    def run_step(command, log, name, *, timeout):
        _require_fence(fence)
        _require_within_bound(deadline)
        return _run_step(command, log, name, timeout=timeout)

    return run_step


def _terminal_error(error: str | None, fence: threading.Event | None) -> str | None:
    """A fence lost during the last step is recorded, never overwritten by success."""
    if error is not None:
        return error
    if fence is not None and fence.is_set():
        return "OperationalOwnershipError: owner_fence_lost"
    return None


def run_nightly_body(
    *, ownership: OwnerLease | None, fence: threading.Event | None = None
) -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    _require_nightly_owner(ownership)
    deadline = time.monotonic() + nightly_budget.worst_case_seconds()
    run_step = fenced_step_runner(fence, deadline)

    result = _PassResult(1)
    terminal_error = None
    try:
        result = _nightly_pass(today, run_step, ownership, fence, deadline)
        return 1 if result.failures else 0
    except Exception as exc:
        terminal_error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        _record_result_quietly(today, result, _terminal_error(terminal_error, fence))


def _nightly_pass(
    today: str,
    run_step,
    ownership: OwnerLease | None,
    fence,
    deadline: float | None = None,
) -> _PassResult:
    runtime_roots.reports_dir().mkdir(parents=True, exist_ok=True)
    log = StepLog(_nightly_logger(runtime_roots.reports_dir() / f"nightly-{today}.md"))
    log(f"=== Nightly consolidation pass — {today} ===")
    failures = nightly_steps(run_step, log, ownership)
    _require_fence(fence)
    _require_within_bound(deadline)
    failures += int(not _housekeeping(log, "pruning", prune_reports))
    # An update is never a reason to fail the night; its failure is named only.
    _housekeeping(log, "update", update_code)
    status = _nightly_completion_status(failures, log.deferred)
    log(f"=== Nightly pass {status} (failures={failures}, deferred={log.deferred}) ===")
    return _PassResult(failures, tuple(log.deferred))


def _housekeeping(log, label: str, step) -> bool:
    """Run a step after the counted ones; its exception is named, never the night's end.

    `update_code` promised never to fail the night, and a busy state lock in its
    one `update_state` did exactly that (audit 2026-09-26, regress 8,
    docs/research/2026-09-26-a-housekeeping-step-names-its-failure.md).
    """
    try:
        step(log)
    except Exception as exc:  # noqa: BLE001 - named in the log, counted by the caller
        log(f"  {label} failed: {describe_error(exc)}")
        return False
    return True


def _record_result_quietly(today: str, result: _PassResult, error: str | None) -> None:
    try:
        record_nightly_result(today, result.failures or int(error is not None), error, deferred=result.deferred)
    except Exception as exc:
        print(f"scheduled_nightly: could not record result: {exc}", file=sys.stderr)


def run_nightly(
    *,
    ownership: OwnerLease | None,
    registry: object | None = None,
) -> int:
    """Run the pass, refreshing its nightly lease while it runs."""
    if ownership is None:
        return run_nightly_body(ownership=None)
    lost = threading.Event()
    with heartbeat_owner(ownership, registry=registry, lost=lost):
        return run_nightly_body(ownership=ownership, fence=lost)


def write_marker(marker: Path) -> bool:
    """Create the marker exclusively and stamp it with this process.

    The descriptor is binary so that Windows writes the payload's newlines as
    they are. Research:
    docs/research/2026-09-18-a-payload-is-written-as-the-bytes-it-is.md
    """
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(str(marker), flags)
    except FileExistsError:
        return False
    try:
        write_all(descriptor, marker_payload())
    finally:
        os.close(descriptor)
    return True


def marker_payload() -> bytes:
    """The PID, and the identity that tells this process from the next one to
    carry its number.

    Research: docs/research/2026-09-17-a-lock-names-the-process-not-only-its-number.md
    """
    return f"{os.getpid()}\n{_own_identity()}\n".encode("ascii", errors="replace")


def _own_identity() -> str:
    """This process's start identity, empty when the probe cannot settle it."""
    try:
        return process_liveness.process_start_identity(os.getpid()) or ""
    except (OSError, ValueError):
        return ""


def _marker_owner(payload: bytes) -> tuple[int, str] | None:
    """(PID, identity) a marker records; one written before this release has none."""
    lines = payload.decode("utf-8", errors="replace").strip().splitlines()
    if not lines:
        return None
    try:
        pid = int(lines[0].strip())
    except ValueError:
        return None
    return (pid, lines[1].strip() if len(lines) > 1 else "")


def _abandoned_marker_bytes(marker: Path) -> bytes | None:
    """The bytes of a marker whose owner process is gone; None while it lives.

    Liveness is the process, never the age (the compile-lock note of
    2026-09-10); this legacy marker serves only vaults without a V3
    coordinator, where the registry's reclaim is unavailable.
    """
    try:
        payload = marker.read_bytes()
    except OSError:
        return None
    owner = _marker_owner(payload)
    if owner is None or process_liveness.owner_alive(*owner):
        return None
    return payload


def _steal_marker(marker: Path) -> bool:
    from memory_state import retire_stale_lock

    judged = _abandoned_marker_bytes(marker)
    if judged is None or not retire_stale_lock(marker, judged):
        return False
    return write_marker(marker)


def _acquire_legacy_maintenance_marker() -> Path | None:
    marker = runtime_roots.state_root() / "run/maintenance.lock"
    marker.parent.mkdir(parents=True, exist_ok=True)
    if write_marker(marker):
        return marker
    return marker if _steal_marker(marker) else None


def _release_legacy_maintenance_marker(marker: Path) -> None:
    """Remove the marker only while it is still the one this process wrote."""
    try:
        if _marker_owner(marker.read_bytes()) == (os.getpid(), _own_identity()):
            marker.unlink()
    except OSError:
        pass


@dataclass(frozen=True)
class ScheduledFence:
    """What a scheduled pass holds: the canonical lease on an adopted vault, the
    legacy marker on a vault without a V3 coordinator."""

    lease: OwnerLease | None
    registry: object | None
    release: Callable[[], None]


def _canonical_fence(role: str, registry) -> ScheduledFence:
    lease, marker = acquire_scheduled_owner(role, state_root=runtime_roots.state_root(), registry=registry)

    def release() -> None:
        # A fence already lost or taken over is reported, not raised again:
        # the body recorded the loss, and there is nothing left to release.
        try:
            release_marker_owner(lease, marker, registry=registry)
        except OperationalOwnershipError as exc:
            print(f"scheduled_nightly: fence not released ({exc.code})", file=sys.stderr)

    return ScheduledFence(lease, registry, release)


def _legacy_fence() -> ScheduledFence | None:
    marker = _acquire_legacy_maintenance_marker()
    if marker is None:
        return None
    return ScheduledFence(None, None, lambda: _release_legacy_maintenance_marker(marker))


def adopted_ownership_registry(vault: Path, state_root: Path) -> OwnershipRegistry | None:
    """The adopted coordinator's registry, or None on a vault without V3 adoption.

    The same rule the doctor applies: an adopted vault holds its owners in the
    active `markdown-transactions-v3.sqlite3`, not in the pre-adoption candidate.
    """
    coordinator = active_or_legacy_coordinator(
        Path(vault), Path(state_root)
    )
    if getattr(coordinator, "_database_contract", None) is None:
        return None
    return coordinator._ownership_registry()  # noqa: SLF001 - the coordinator's own rule


def take_scheduled_fence(role: str) -> ScheduledFence | None:
    """The fence for `role`: canonical where the vault is adopted, legacy otherwise.

    None means another pass holds the legacy marker. A canonical refusal raises
    `OperationalOwnershipError` with the registry's reason.
    Decision: knowledge/notes/nightly-takes-the-canonical-fence-decision.md
    """
    registry = adopted_ownership_registry(runtime_roots.vault_root(), runtime_roots.state_root())
    if registry is None:
        return _legacy_fence()
    return _canonical_fence(role, registry)


def record_scheduled_skip(today: str, reason: str) -> None:
    print(f"scheduled_nightly: maintenance already running ({reason}), skipping.", file=sys.stderr)
    try:
        _record_nightly_skip(today, reason)
    except Exception as exc:
        print(f"scheduled_nightly: could not record skip: {exc}", file=sys.stderr)


def record_scheduled_failure(today: str, exc: BaseException) -> None:
    """A pass that failed before it started is still a failed pass.

    The adoption refusal of 2026-09-17 was raised while taking the fence, so
    the process exited 1 for six nights while `run/state.json` kept saying
    `success`, and session start never named it. Recorded here the way the
    pass records its own failures, so the health checks and session start read
    one field. See `docs/research/2026-09-23-the-rest-of-the-live-audit.md`.
    """
    from secret_redact import describe_error_chain

    print(f"scheduled_nightly: could not take the fence: {describe_error(exc)}", file=sys.stderr)
    try:
        record_nightly_result(today, 1, error=describe_error_chain(exc))
    except Exception as failure:  # noqa: BLE001 - the original error is what matters
        print(f"scheduled_nightly: could not record failure: {failure}", file=sys.stderr)


def main(argv: list[str] | tuple[str, ...] = ()) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        fence = take_scheduled_fence("nightly")
    except OperationalOwnershipError as exc:
        record_scheduled_skip(today, exc.code)
        return 0
    except Exception as exc:
        record_scheduled_failure(today, exc)
        raise
    if fence is None:
        record_scheduled_skip(today, "maintenance_lock_held")
        return 0
    try:
        return run_nightly(ownership=fence.lease, registry=fence.registry)
    finally:
        fence.release()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
