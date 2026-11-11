"""The nightly pass's steps and how long the slowest possible run of them takes."""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from llm_wiki import runtime_roots
from llm_wiki.step_budgets import (
    RECLAIM_STEP_SECONDS,
    REFRESH_ALL_BUDGET_SECONDS,
    RETIRE_BUDGET_SECONDS,
)
from settings import setting_value

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


@dataclass(frozen=True)
class Step:
    """One nightly subprocess step: what to announce, run, and how long to wait."""

    message: str
    label: str
    command: list[str]
    timeout: float


def script(name: str) -> list[str]:
    return [sys.executable, str(runtime_roots.vault_root() / "scripts" / name)]


def capture_adoption_step() -> Step:
    """Dispatch intents published durably but never given a task.

    The capture worker sweeps these too, but it only runs when a capture wakes
    it. If the failure that orphaned an intent is the same one stopping captures
    from finishing, nothing would ever run the sweeper — so recovery cannot
    depend on another capture arriving. This is the pass that does not.
    """
    return Step(
        "adopting undispatched capture intents...",
        "capture_adoption",
        script("capture_adoption.py"),
        120,
    )


def reclaim_step() -> Step:
    """Finish what a hook is too impatient to finish, before anything else runs.

    A hook drains the project-checkpoint queue with a 0.5 s state-lock budget
    because a person is waiting on it; a backlog therefore outlives every hook
    and grows `run/state.json`, which makes the next hook slower still. Nothing
    else in this pass breaks that loop, and every later step reads the state
    file this one shrinks.
    """
    return Step(
        "reclaiming runtime state...",
        "reclaim",
        script("reclaim_runtime_state.py"),
        RECLAIM_STEP_SECONDS,
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


def head_arrival_time(root: Path | None = None) -> str | None:
    """When HEAD last moved in this checkout (its reflog), else its commit time.

    The commit time is earlier than the fast-forward that brought the fix here,
    so captures that died on the old code in between never got their redrive
    (audit 2026-09-26 B-28,
    docs/research/2026-09-26-a-fix-arrives-when-the-checkout-moves.md).
    """
    root = root or runtime_roots.vault_root()
    selector = _git_line(root, "reflog", "-1", "--date=iso-strict", "--format=%gd", "HEAD")
    if selector is not None and selector.startswith("HEAD@{") and selector.endswith("}"):
        return selector[len("HEAD@{") : -1]
    return _git_line(root, "log", "-1", "--format=%cI", "HEAD")


def _dead_capture_redrive_steps() -> list[Step]:
    """Give dead captures their one redrive once the code changed after they died.

    Runs before the queue worker so the redriven captures are worked the same
    night. No readable HEAD means no known code change, so no step. See
    `docs/research/2026-09-25-a-dead-capture-gets-its-second-chance-after-a-fix.md`.
    """
    changed_after = head_arrival_time()
    if changed_after is None:
        return []
    return [
        Step(
            "redriving dead captures the code has changed since...",
            "dead_capture_redrive",
            script("memory_queue.py") + ["redrive-dead-captures", "--changed-after", changed_after],
            120,
        )
    ]


def intake_steps() -> list[Step]:
    """The steps that take in what arrived since the last pass, in order."""
    return [
        capture_adoption_step(),
        reclaim_step(),
        *_dead_capture_redrive_steps(),
        queue_step(),
        episode_step(),
    ]


def queue_step() -> Step:
    return Step(
        "working deferred memory queue...",
        "work",
        script("memory_queue.py")
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


def episode_step() -> Step:
    """Consolidate every day still pending before compile reads the daily log.

    Sessions are kept verbatim whatever the classifier thought of them; this is
    where a day of them becomes durable knowledge, in the window where nobody is
    waiting. Every promoted item must quote the record it came from. It used to
    take yesterday only, so a day that failed once was never read again; five
    such days were found on 2026-09-14. See
    `docs/research/2026-09-14-a-day-that-failed-is-tried-again.md`.
    """
    return Step(
        "consolidating pending sessions...",
        "episodes",
        script("episode_consolidation.py")
        + ["--all-pending", "--budget-seconds", str(EPISODE_BUDGET_SECONDS)],
        EPISODE_BUDGET_SECONDS + provider_margin_seconds(),
    )


def compile_step(root: Path | None = None) -> Step:
    """The child compiles, then refreshes its generation before returning."""
    refresh = setting_value("generation.post_compile_seconds", runtime_roots.vault_root() if root is None else root)
    timeout = compile_wait_seconds() + refresh + STEP_START_MARGIN_SECONDS
    return Step("compiling pending sources...", "compile",
                 script("compile_memory.py") + ["--trigger", "auto"], timeout)


def fact_keys_step() -> Step:
    """Key the user turns of new daily entries, so retrieval can find a fact by its statement.

    One provider call per twenty-five turns, in this window where nobody is
    waiting; a turn is keyed once. See `fact_keys`.
    """
    from llm_wiki.step_budgets import (
        DEFAULT_BUDGET_SECONDS,
    )

    return Step(
        "keying new user turns...",
        "fact_keys",
        script("fact_keys.py"),
        int(DEFAULT_BUDGET_SECONDS) + provider_margin_seconds(),
    )


def _checkpoint_step() -> Step:
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
    return Step(
        "clearing checkpoints nothing will settle...",
        "checkpoints",
        script("repair_orphaned_checkpoint_names.py"),
        120,
    )


def _own_calls_step() -> Step:
    """Retire the transcripts the memory's own provider calls left outside the vault.

    `--no-session-persistence` stopped new ones on 2026-09-14; the 1 082 old ones
    waited for a hand until 2026-09-23. Nothing the memory leaves behind is the
    operator's chore. See `docs/research/2026-09-23-the-memory-retires-its-own-residue.md`.
    """
    return Step(
        "retiring the memory's own call transcripts...",
        "own_calls",
        script("retire_own_call_transcripts.py"),
        60,
    )


def _benchmark_runs_step() -> Step:
    """Retire benchmark run directories untouched for a month; dataset caches stay.

    1.5 GB under `cache/benchmarks/` on 2026-09-24 with nothing removing any of it.
    See `docs/research/2026-09-24-every-store-has-a-bound.md`.
    """
    return Step(
        "retiring old benchmark run directories...",
        "benchmark_runs",
        script("retire_benchmark_runs.py"),
        120,
    )


def _lsp_evidence_step() -> Step:
    """Retire LSP failure roots older than two weeks beyond the newest twenty.

    They were "left for the operator", who had no command for them: 80 roots on
    2026-09-23. See `docs/research/2026-09-23-the-rest-of-the-live-audit.md`.
    """
    return Step(
        "retiring old LSP failure evidence...",
        "lsp_evidence",
        script("retire_lsp_evidence.py"),
        60,
    )


def post_compile_steps() -> list[Step]:
    return [
        Step(
            "structural lint...",
            "lint",
            script("lint_memory.py"),
            120,
        ),
        Step(
            "repairing owed backlinks...",
            "backlinks",
            script("repair_backlinks.py") + ["--apply"],
            120,
        ),
        Step(
            # Issue #24, section A: the timer half of the background refresh.
            # Every registered repository whose checkout still exists is looked
            # at and rebuilt incrementally only when its sources changed, each
            # under its own per-repository fence.
            "refreshing registered repository generations...",
            "repositories",
            script("repository_index.py")
            + ["refresh-all", "--budget-seconds", str(REFRESH_ALL_BUDGET_SECONDS)],
            REFRESH_ALL_BUDGET_SECONDS + STEP_START_MARGIN_SECONDS,
        ),
        Step(
            # Issue #24, section D1: a foreign generation is never activated,
            # so the pruner below reports it pending and keeps it forever.
            # This retires, per checkout, every generation of a checkout that
            # is gone or marked not indexed and all but the newest two of the
            # rest, each repository under its own fence.
            "retiring repository generations no checkout reads...",
            "repository_retention",
            script("repository_index.py") + ["retire"],
            RETIRE_BUDGET_SECONDS + STEP_START_MARGIN_SECONDS,
        ),
        Step(
            # Every refresh publishes a new immutable generation and nothing
            # removed the old ones: five in one day, 1.05 GB, on the vault of
            # issue #29. The pruner keeps the active generation and one ancestor.
            "pruning superseded evidence generations...",
            "prune_generations",
            script("prune_generations.py")
            + ["--apply", "--budget-seconds", str(PRUNE_STEP_SECONDS - STEP_START_MARGIN_SECONDS)],
            PRUNE_STEP_SECONDS,
        ),
        _checkpoint_step(),
        _lsp_evidence_step(),
        _own_calls_step(),
        _benchmark_runs_step(),
        Step(
            # The read path loads weights local-only; a cache that lacks the
            # two pinned models answers by words alone. Present files are not
            # fetched again, so this is a no-op on every night but the first.
            "fetching missing model weights...",
            "models",
            script("install_models.py"),
            1800,
        ),
    ]


# How long a nightly pass follows a running compile before deferring the
# steps that read its output. Five minutes was the old bound; issue #21
# measured a healthy compile of one daily log at 6.5 minutes through the
# Claude CLI and the pass recorded it as two failures. Thirty minutes is
# the new floor, and an operator sets `MEMORY_COMPILE_WAIT_SECONDS`.
COMPILE_WAIT_SECONDS = 1800.0


COMPILE_WAIT_ENV = "MEMORY_COMPILE_WAIT_SECONDS"


def compile_wait_seconds() -> float:
    """`MEMORY_COMPILE_WAIT_SECONDS`, else `COMPILE_WAIT_SECONDS`: how long one compile may run."""
    raw = os.environ.get(COMPILE_WAIT_ENV, "").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return COMPILE_WAIT_SECONDS


# `maintenance_helpers.wait_for_compile_idle`: three tries, ten seconds each.
COMPILE_IDLE_WAIT_SECONDS = 30


# The two tail tasks are bounded by work rather than by wall time: the telemetry
# compaction deletes at most `retrieval_telemetry.DEFAULT_MAX_DELETE` rows under
# a 5 s busy timeout, and the retention pass looks at at most
# `retention.report_files` reports (a setting; raising it far lengthens this tail). This is what the pass
# allows them together; outliving it is caught at the next step boundary.
MAINTENANCE_TAIL_BUDGET_SECONDS = 120


def worst_case_seconds(root: Path | None = None) -> float:
    """The longest a pass can run by its own bounds, as configured right now.

    Every step's timeout (with the margin a provider call really needs), every
    wait as the environment sets it, the generation and health budgets, the
    checkout update by its own timeouts, and the tail allowance. The pass stops
    itself at this bound, and a scheduler's limit sits above it. See
    `docs/research/2026-09-14-the-scheduler-outlasts-the-pass.md` and
    `docs/research/2026-09-18-a-pass-that-knows-how-long-it-can-be.md`.
    """
    from llm_wiki.step_budgets import (
        WORST_CASE_SECONDS as UPDATE_SECONDS,
    )

    steps = [*intake_steps(), fact_keys_step(), compile_step(root), *post_compile_steps()]
    waits = COMPILE_IDLE_WAIT_SECONDS
    selected_root = runtime_roots.vault_root() if root is None else root
    budgets = setting_value("generation.nightly_seconds", selected_root) + setting_value("doctor.nightly_seconds", selected_root)
    tail = MAINTENANCE_TAIL_BUDGET_SECONDS + UPDATE_SECONDS
    return float(sum(step.timeout for step in steps) + waits + budgets + tail)
