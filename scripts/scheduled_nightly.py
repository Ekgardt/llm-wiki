"""Nightly consolidation pass — started at 03:00 by the installed scheduler.

The scheduler is Task Scheduler on Windows, a user LaunchAgent on macOS, a
user systemd timer on Linux, cron as the explicit degraded fallback
(`install_control.py`). The pass runs, in order: capture-intent adoption,
runtime reclaim, the deferred memory queue, pending session consolidation;
the compile (spawned through `maybe_compile`, followed until it finishes or
the wait bound passes) and user-turn keying; then the steps that read the
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

import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import maybe_compile  # noqa: E402
import process_liveness  # noqa: E402
from doctor import (  # noqa: E402
    DEFAULT_GENERATION_SOURCE_LIMIT,
    run_generation_maintenance,
)
from maintenance_helpers import prune_maintenance_output, trim_scheduler_logs  # noqa: E402
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
    OperationalOwnershipError,
    OwnerLease,
    acquire_scheduled_owner,
    adopted_ownership_registry,
    heartbeat_owner,
    release_marker_owner,
)
from repository_index import REFRESH_ALL_BUDGET_SECONDS  # noqa: E402
from repository_retention import RETIRE_BUDGET_SECONDS  # noqa: E402
from secret_redact import describe_error  # noqa: E402

# How long the nightly pass will spend rebuilding the evidence generation.
# The interactive default is one minute, which is the right bound for a doctor
# run someone is waiting on. A nightly window is not that: on this vault a full
# build of 762 sources takes 98 seconds, so a one-minute bound deferred every
# night and the generation was never rebuilt at all. The unit itself has no
# start timeout, so the only bound that matters is this one.
NIGHTLY_GENERATION_BUDGET_SECONDS = 15 * 60
# The refresh of every registered foreign repository shares one bound, the
# child's own (`repository_index.REFRESH_ALL_BUDGET_SECONDS`); the refresh is
# incremental (measured 2026-09-10: 16 s after one edited file in a 1 022-file
# repository, against 60 s for the full build), and a repository that does
# not fit is deferred to the next night, never half-built. The step's kill
# timeout sits above that budget by a margin for interpreter start-up and
# the deferral report, so the child's graceful deferral runs before the
# parent's kill (audit OPS-10).
STEP_START_MARGIN_SECONDS = 120
# The prune's kill timeout; its own budget ends a margin before it. See
# `docs/research/2026-09-14-a-prune-inside-its-step.md`.
PRUNE_STEP_SECONDS = 300


def _generation_result() -> dict:
    """The shared builder takes its own `repair` owner; the pass passes none."""
    return run_generation_maintenance(
        root=ROOT,
        state_root=STATE_ROOT,
        time_budget_seconds=NIGHTLY_GENERATION_BUDGET_SECONDS,
        max_sources=DEFAULT_GENERATION_SOURCE_LIMIT,
    )


def _refresh_generation(log) -> int:
    """Run the shared bounded builder under its fenced maintenance owner."""
    result = _generation_result()
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


def _utc_now() -> str:
    """Every instant in the state is UTC with its offset; a date stays the local calendar day.

    `skipped_at` and `failed_at` were local and `last_nightly_at` UTC, and doctor
    compared them as strings (audit C-30,
    docs/research/2026-09-25-the-scheduler-state-keeps-one-clock.md).
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _record_nightly_result(today: str, failures: int, error: str | None = None) -> None:
    """Release today's catchup lease and persist the terminal result."""
    timestamp = _utc_now()

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
            state["last_nightly_at"] = timestamp
            state.pop("last_nightly_failure", None)

    update_state(_mutate)


def _record_nightly_skip(today: str, reason: str) -> None:
    """Release today's claim without replacing the last execution result."""
    timestamp = _utc_now()

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

    def __call__(self, message: str) -> None:
        self._log(message)

    def step(self, message: str) -> None:
        self._count += 1
        self._log(f"Step {self._count}: {message}")


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
        "adopting undispatched capture intents...",
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
        "reclaiming runtime state...",
        "reclaim",
        _script("reclaim_runtime_state.py"),
        180,
    )


# The queue worker's wall time ends a margin before this step is killed. See
# `docs/research/2026-09-14-no-task-is-claimed-to-be-killed.md`.
QUEUE_STEP_SECONDS = 600


# How long reading the checkout's HEAD commit time may take.
HEAD_TIME_TIMEOUT_SECONDS = 10


def _git_line(root: Path, *arguments: str) -> str | None:
    """One line of a bounded git answer, or None when git cannot give it."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *arguments],
            capture_output=True,
            text=True,
            timeout=HEAD_TIME_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def head_arrival_time(root: Path = ROOT) -> str | None:
    """When HEAD last moved in this checkout (its reflog), else its commit time.

    The commit time is earlier than the fast-forward that brought the fix here,
    so captures that died on the old code in between never got their redrive
    (audit 2026-09-26 B-28,
    docs/research/2026-09-26-a-fix-arrives-when-the-checkout-moves.md).
    """
    selector = _git_line(root, "reflog", "-1", "--date=iso-strict", "--format=%gd", "HEAD")
    if selector is not None and selector.startswith("HEAD@{") and selector.endswith("}"):
        return selector[len("HEAD@{") : -1]
    return _git_line(root, "log", "-1", "--format=%cI", "HEAD")


def _dead_capture_redrive_steps() -> list[_Step]:
    """Give dead captures their one redrive once the code changed after they died.

    Runs before the queue worker so the redriven captures are worked the same
    night. No readable HEAD means no known code change, so no step. See
    `docs/research/2026-09-25-a-dead-capture-gets-its-second-chance-after-a-fix.md`.
    """
    changed_after = head_arrival_time()
    if changed_after is None:
        return []
    return [
        _Step(
            "redriving dead captures the code has changed since...",
            "dead_capture_redrive",
            _script("memory_queue.py") + ["redrive-dead-captures", "--changed-after", changed_after],
            120,
        )
    ]


def _intake_steps() -> list[_Step]:
    """The steps that take in what arrived since the last pass, in order."""
    return [
        _capture_adoption_step(),
        _reclaim_step(),
        *_dead_capture_redrive_steps(),
        _queue_step(),
        _episode_step(),
    ]


def _queue_step() -> _Step:
    return _Step(
        "working deferred memory queue...",
        "work",
        _script("memory_queue.py")
        + ["work", "--max-seconds", str(QUEUE_STEP_SECONDS - STEP_START_MARGIN_SECONDS)],
        QUEUE_STEP_SECONDS,
    )


# The consolidation starts no new batch after this; one batch (a model call and its
# write) fits in the margin before the step's kill. See
# `docs/research/2026-09-14-every-budget-inside-its-step.md`.
EPISODE_BUDGET_SECONDS = 180


def provider_margin_seconds() -> int:
    """What a step must leave after its child's budget for one model call.

    The child stops starting work at its budget with one call possibly in
    flight, and one call is not one provider: in auto mode it walks the whole
    order. The flat 120 s covered the interpreter and one provider only.
    Research: docs/research/2026-09-18-a-pass-that-knows-how-long-it-can-be.md
    """
    from llm_client import worst_case_call_seconds

    return STEP_START_MARGIN_SECONDS + worst_case_call_seconds()


def _episode_step() -> _Step:
    """Consolidate every day still pending before compile reads the daily log.

    Sessions are kept verbatim whatever the classifier thought of them; this is
    where a day of them becomes durable knowledge, in the window where nobody is
    waiting. Every promoted item must quote the record it came from. It used to
    take yesterday only, so a day that failed once was never read again; five
    such days were found on 2026-09-14. See
    `docs/research/2026-09-14-a-day-that-failed-is-tried-again.md`.
    """
    return _Step(
        "consolidating pending sessions...",
        "episodes",
        _script("episode_consolidation.py")
        + ["--all-pending", "--budget-seconds", str(EPISODE_BUDGET_SECONDS)],
        EPISODE_BUDGET_SECONDS + provider_margin_seconds(),
    )


def _compile_step() -> _Step:
    return _Step(
        "triggering compile (if needed)...",
        "maybe_compile",
        _script("maybe_compile.py"),
        60,
    )


def _fact_keys_step() -> _Step:
    """Key the user turns of new daily entries, so retrieval can find a fact by its statement.

    One provider call per twenty-five turns, in this window where nobody is
    waiting; a turn is keyed once. See `fact_keys`.
    """
    from fact_keys import DEFAULT_BUDGET_SECONDS

    return _Step(
        "keying new user turns...",
        "fact_keys",
        _script("fact_keys.py"),
        int(DEFAULT_BUDGET_SECONDS) + provider_margin_seconds(),
    )


def _checkpoint_step() -> _Step:
    """Clear a checkpoint sequence whose own request is never coming back.

    The design clears a quarantined or reserved sequence the right way: the
    original request arrives again, re-derives the same name, and is given a
    fresh attempt. A session-end checkpoint has no such second arrival — the
    session is over — so a sequence that loses its race stays unsettled and
    blocks every sequence behind it for that project.

    Measured on this vault on 2026-09-07: `llm-wiki` 2214 lost a precondition
    during the benchmark runs and 2215 sat reserved behind it, `another-project` 830
    likewise. Six hundred hook failures accumulated over a day, one per
    session end, and clearing it took a person running a repair script by
    hand — which is the thing this pass exists to stop needing.

    Safe to run every night: it takes only rows no live lease owns, and does
    nothing on a vault that has none.
    """
    return _Step(
        "clearing checkpoints nothing will settle...",
        "checkpoints",
        _script("repair_orphaned_checkpoint_names.py"),
        120,
    )


def _own_calls_step() -> _Step:
    """Retire the transcripts the memory's own provider calls left outside the vault.

    `--no-session-persistence` stopped new ones on 2026-09-14; the 1 082 old ones
    waited for a hand until 2026-09-23. Nothing the memory leaves behind is the
    operator's chore. See `docs/research/2026-09-23-the-memory-retires-its-own-residue.md`.
    """
    return _Step(
        "retiring the memory's own call transcripts...",
        "own_calls",
        _script("retire_own_call_transcripts.py"),
        60,
    )


def _benchmark_runs_step() -> _Step:
    """Retire benchmark run directories untouched for a month; dataset caches stay.

    1.5 GB under `cache/benchmarks/` on 2026-09-24 with nothing removing any of it.
    See `docs/research/2026-09-24-every-store-has-a-bound.md`.
    """
    return _Step(
        "retiring old benchmark run directories...",
        "benchmark_runs",
        _script("retire_benchmark_runs.py"),
        120,
    )


def _lsp_evidence_step() -> _Step:
    """Retire LSP failure roots older than two weeks beyond the newest twenty.

    They were "left for the operator", who had no command for them: 80 roots on
    2026-09-23. See `docs/research/2026-09-23-the-rest-of-the-live-audit.md`.
    """
    return _Step(
        "retiring old LSP failure evidence...",
        "lsp_evidence",
        _script("retire_lsp_evidence.py"),
        60,
    )


def _post_compile_steps() -> list[_Step]:
    return [
        _Step(
            "structural lint...",
            "lint",
            _script("lint_memory.py"),
            120,
        ),
        _Step(
            "repairing owed backlinks...",
            "backlinks",
            _script("repair_backlinks.py") + ["--apply"],
            120,
        ),
        _Step(
            # Issue #24, section A: the timer half of the background refresh.
            # Every registered repository whose checkout still exists is looked
            # at and rebuilt incrementally only when its sources changed, each
            # under its own per-repository fence.
            "refreshing registered repository generations...",
            "repositories",
            _script("repository_index.py")
            + ["refresh-all", "--budget-seconds", str(REFRESH_ALL_BUDGET_SECONDS)],
            REFRESH_ALL_BUDGET_SECONDS + STEP_START_MARGIN_SECONDS,
        ),
        _Step(
            # Issue #24, section D1: a foreign generation is never activated,
            # so the pruner below reports it pending and keeps it forever.
            # This retires, per checkout, every generation of a checkout that
            # is gone or marked not indexed and all but the newest two of the
            # rest, each repository under its own fence.
            "retiring repository generations no checkout reads...",
            "repository_retention",
            _script("repository_index.py") + ["retire"],
            RETIRE_BUDGET_SECONDS + STEP_START_MARGIN_SECONDS,
        ),
        _Step(
            # Every refresh publishes a new immutable generation and nothing
            # removed the old ones: five in one day, 1.05 GB, on the vault of
            # issue #29. The pruner keeps the active generation and one ancestor.
            "pruning superseded evidence generations...",
            "prune_generations",
            _script("prune_generations.py")
            + ["--apply", "--budget-seconds", str(PRUNE_STEP_SECONDS - STEP_START_MARGIN_SECONDS)],
            PRUNE_STEP_SECONDS,
        ),
        _checkpoint_step(),
        _lsp_evidence_step(),
        _own_calls_step(),
        _benchmark_runs_step(),
        _Step(
            # The read path loads weights local-only; a cache that lacks the
            # two pinned models answers by words alone. Present files are not
            # fetched again, so this is a no-op on every night but the first.
            "fetching missing model weights...",
            "models",
            _script("install_models.py"),
            1800,
        ),
    ]


def _run_steps(run_step, log, steps: list[_Step]) -> int:
    """Run each step in order and count the ones that failed."""
    failures = 0
    for step in steps:
        log.step(step.message)
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


COMPILE_DIED_WITHOUT_OUTCOME = "compile exited without recording an outcome"


def _compile_failed_this_pass(
    before: str | None, started_before: str | None = None
) -> str | None:
    """The error of a compile that ran in this pass, or None.

    `maybe_compile` spawns the compile and returns 0 as soon as it is running,
    so the step it belongs to says nothing about the outcome. Waiting for the
    process to stop says nothing either. On 2026-08-22 that let a nightly pass
    report `failures=0` for a night whose compile had died a second in. The
    stamp comparison keeps last night's error out of tonight's count, and a
    compile killed before it could write any outcome is counted by its start
    stamp (docs/research/2026-09-11-the-seven-questions-the-audits-left-open.md).
    """
    state = _safe_state()
    if _compile_died_this_pass(state, started_before):
        return COMPILE_DIED_WITHOUT_OUTCOME
    return _recorded_compile_error(state, before)


def _recorded_compile_error(state: dict, before: str | None) -> str | None:
    finished = state.get("last_compile_finished_at")
    if not finished or str(finished) == before:
        return None
    if state.get("last_compile_status") != "error":
        return None
    return str(state.get("last_compile_error") or "unknown")


def _compile_died_this_pass(state: dict, started_before: str | None) -> bool:
    """A compile started in this pass, is not running, and never left `running`."""
    started = state.get("last_compile_started_at")
    if not started or str(started) == started_before:
        return False
    return state.get("last_compile_status") == "running" and not _compile_running()


def _last_compile_finished() -> str | None:
    finished = _safe_state().get("last_compile_finished_at")
    return str(finished) if finished else None


def _last_compile_started() -> str | None:
    started = _safe_state().get("last_compile_started_at")
    return str(started) if started else None


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


def _post_compile_pass(run_step, log) -> int:
    failures = _run_steps(run_step, log, _post_compile_steps())

    # refresh one immutable generation under the shared fence.
    log.step("refreshing immutable evidence generation...")
    failures += _refresh_generation(log)

    # compact disposable telemetry without touching knowledge.
    log.step("compacting retrieval telemetry...")
    _compact_telemetry(log)

    # one full health report, read at session start instead of measured there.
    log.step("writing the health report...")
    _write_health_report(log)
    return failures


# Issue #23.5: session start allows the doctor 0.1 s and said "not measured"
# every morning. The night has the time; the morning reads what it wrote.
HEALTH_REPORT_NAME = "doctor-report.json"
HEALTH_REPORT_BUDGET_SECONDS = 60
# `maintenance_helpers.wait_for_compile_idle`: three tries, ten seconds each.
COMPILE_IDLE_WAIT_SECONDS = 30


# The two tail tasks are bounded by work rather than by wall time: the telemetry
# compaction deletes at most `retrieval_telemetry.DEFAULT_MAX_DELETE` rows under
# a 5 s busy timeout, and the retention pass looks at at most
# `maintenance_helpers.REPORT_RETENTION_FILES` reports. This is what the pass
# allows them together; outliving it is caught at the next step boundary.
MAINTENANCE_TAIL_BUDGET_SECONDS = 120


def worst_case_seconds() -> float:
    """The longest a pass can run by its own bounds, as configured right now.

    Every step's timeout (with the margin a provider call really needs), every
    wait as the environment sets it, the generation and health budgets, the
    checkout update by its own timeouts, and the tail allowance. The pass stops
    itself at this bound, and a scheduler's limit sits above it. See
    `docs/research/2026-09-14-the-scheduler-outlasts-the-pass.md` and
    `docs/research/2026-09-18-a-pass-that-knows-how-long-it-can-be.md`.
    """
    from self_update import WORST_CASE_SECONDS as UPDATE_SECONDS

    steps = [*_intake_steps(), _compile_step(), _fact_keys_step(), *_post_compile_steps()]
    waits = COMPILE_IDLE_WAIT_SECONDS + _compile_wait_seconds()
    budgets = NIGHTLY_GENERATION_BUDGET_SECONDS + HEALTH_REPORT_BUDGET_SECONDS
    tail = MAINTENANCE_TAIL_BUDGET_SECONDS + UPDATE_SECONDS
    return float(sum(step.timeout for step in steps) + waits + budgets + tail)


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
        log(f"  health report skipped: {describe_error(exc)}")


def _update_code(log) -> None:
    """Advance the checkout last, so changed code takes effect next pass.

    An update is never a reason to fail the night: a diverged branch, an offline
    machine and a file the owner is editing are ordinary states, and the step
    names which one it met.
    """
    from self_update import update_checkout

    log.step("updating the vault code...")
    outcome = update_checkout(ROOT)
    update_state(lambda state: state.__setitem__("last_update", update_record(outcome)))
    log(f"  update: {outcome['status']} ({outcome.get('reason') or 'none'})")
    if outcome.get("detail"):
        log(f"  update: {outcome['detail']}")
    _log_update_aftermath(log, outcome)


UPDATE_RECORD_FIELDS = ("status", "reason", "dependencies", "resources")


def update_record(outcome: dict) -> dict:
    """What the code update did, kept for doctor; before, only the night's log knew.

    See `docs/research/2026-09-25-the-scheduler-says-what-the-night-could-not-do.md`.
    """
    record = {field: outcome.get(field) for field in UPDATE_RECORD_FIELDS}
    record["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return record


def _log_update_aftermath(log, outcome: dict) -> None:
    """What the update brought in but did not bring into force.

    See `docs/research/2026-09-17-an-update-says-what-it-did-not-bring-into-force.md`.
    """
    if outcome.get("status") != "updated":
        return
    extras = ", ".join(outcome.get("extras") or ()) or "none"
    log(f"  update: dependencies {outcome.get('dependencies')} with extras: {extras}")
    log(f"  update: owned resources {outcome.get('resources')}")


def _prune_reports(log) -> None:
    """Retention over every maintenance report family and its artifacts."""
    log.step("pruning maintenance reports and artifacts...")
    log(f"  pruned {prune_maintenance_output()} old file(s)")
    log(f"  trimmed {trim_scheduler_logs()} byte(s) from the scheduler logs")


def _nightly_steps(run_step, log, _ownership: OwnerLease | None = None) -> int:
    failures = _run_steps(run_step, log, _intake_steps())

    # The compile step must not be skipped just because a hook-triggered one runs.
    _wait_for_compile_idle(log)
    before = _last_compile_finished()
    started_before = _last_compile_started()
    failures += _run_steps(run_step, log, [_compile_step(), _fact_keys_step()])

    log.step("waiting for compile to finish...")
    if not _wait_compile_finished():
        # A compile that is still running is deferred, not failed: its outcome
        # is unknown, and the steps that read its output wait for the next
        # pass. Counting it as a failure turned a slow healthy night red (#21).
        log("WARNING: compile still running past the wait bound — lint/index/graph deferred to the next pass")
        log("  a service manager that owns this pass (systemd) stops that compile when the pass exits")
        _remember_deferred_compile(log)
        return failures
    failures += _report_compile_outcome(log, before, started_before) + _report_deferred_loss(log)
    return failures + _post_compile_pass(run_step, log)


DEFERRED_COMPILE_KEY = "nightly_deferred_compile"


def _remember_deferred_compile(log) -> None:
    """Keep the deferred compile's start stamp, so the next pass can miss it.

    Under systemd the unit ends here and the compile ends with it; the loss used
    to be invisible, because the next pass compares against its own start stamp.
    Research: docs/research/2026-09-18-a-pass-that-knows-how-long-it-can-be.md
    """
    started = _last_compile_started()
    if started is None:
        return
    try:
        update_state(lambda state: state.__setitem__(DEFERRED_COMPILE_KEY, started))
    except Exception as exc:  # noqa: BLE001 - a note about a loss is never a failure
        log(f"  deferred compile not recorded: {describe_error(exc)}")


def _report_deferred_loss(log) -> int:
    """Report a compile a previous pass deferred that never recorded an outcome."""
    state = _safe_state()
    deferred = state.get(DEFERRED_COMPILE_KEY)
    if not deferred:
        return 0
    lost = str(deferred) == str(state.get("last_compile_started_at") or "") and (
        state.get("last_compile_status") == "running"
    )
    _forget_deferred_compile(log)
    if not lost:
        return 0
    log(f"  compile: FAILED — a compile deferred at {deferred} never finished")
    return 1


def _forget_deferred_compile(log) -> None:
    try:
        update_state(lambda state: state.pop(DEFERRED_COMPILE_KEY, None))
    except Exception as exc:  # noqa: BLE001 - as above
        log(f"  deferred compile not cleared: {describe_error(exc)}")


def _report_compile_outcome(log, before: str | None, started_before: str | None = None) -> int:
    error = _compile_failed_this_pass(before, started_before)
    if error is None:
        return 0
    log(f"  compile: FAILED — {error}")
    return 1


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


def _fenced_step_runner(fence: threading.Event | None, deadline: float | None = None):
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


def _run_nightly_body(
    *, ownership: OwnerLease | None, fence: threading.Event | None = None
) -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    _require_nightly_owner(ownership)
    deadline = time.monotonic() + worst_case_seconds()
    run_step = _fenced_step_runner(fence, deadline)

    failures = 1
    terminal_error = None
    try:
        failures = _nightly_pass(today, run_step, ownership, fence, deadline)
        return 1 if failures else 0
    except Exception as exc:
        terminal_error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        _record_result_quietly(today, failures, _terminal_error(terminal_error, fence))


def _nightly_pass(
    today: str,
    run_step,
    ownership: OwnerLease | None,
    fence,
    deadline: float | None = None,
) -> int:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    log = StepLog(_nightly_logger(REPORTS_DIR / f"nightly-{today}.md"))
    log(f"=== Nightly consolidation pass — {today} ===")
    failures = _nightly_steps(run_step, log, ownership)
    _require_fence(fence)
    _require_within_bound(deadline)
    _prune_reports(log)
    _update_code(log)
    log(f"=== Nightly pass complete (failures={failures}) ===")
    return failures


def _record_result_quietly(today: str, failures: int, error: str | None) -> None:
    try:
        _record_nightly_result(today, failures or int(error is not None), error)
    except Exception as exc:
        print(f"scheduled_nightly: could not record result: {exc}", file=sys.stderr)


def run_nightly(
    *,
    ownership: OwnerLease | None,
    registry: object | None = None,
) -> int:
    """Run the pass, refreshing its nightly lease while it runs."""
    if ownership is None:
        return _run_nightly_body(ownership=None)
    lost = threading.Event()
    with heartbeat_owner(ownership, registry=registry, lost=lost):
        return _run_nightly_body(ownership=ownership, fence=lost)


def _write_marker(marker: Path) -> bool:
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
        os.write(descriptor, marker_payload())
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
    return _write_marker(marker)


def _acquire_legacy_maintenance_marker() -> Path | None:
    marker = STATE_ROOT / "run/maintenance.lock"
    marker.parent.mkdir(parents=True, exist_ok=True)
    if _write_marker(marker):
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
    lease, marker = acquire_scheduled_owner(role, state_root=STATE_ROOT, registry=registry)

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


def take_scheduled_fence(role: str) -> ScheduledFence | None:
    """The fence for `role`: canonical where the vault is adopted, legacy otherwise.

    None means another pass holds the legacy marker. A canonical refusal raises
    `OperationalOwnershipError` with the registry's reason.
    Decision: knowledge/notes/nightly-takes-the-canonical-fence-decision.md
    """
    registry = adopted_ownership_registry(ROOT, STATE_ROOT)
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
        _record_nightly_result(today, 1, error=describe_error_chain(exc))
    except Exception as failure:  # noqa: BLE001 - the original error is what matters
        print(f"scheduled_nightly: could not record failure: {failure}", file=sys.stderr)


def main() -> int:
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
    raise SystemExit(main())
