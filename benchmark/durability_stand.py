"""Zero-silent-loss durability stand: trial vaults, kills, recovery, audit.

One trial = one freshly adopted temp vault, one session_end capture driven
through the real adapter -> queue -> worker chain in subprocesses
(`benchmark/durability_child.py`), one SIGKILL at a named pipeline boundary,
then the documented recovery (repeated capture-worker runs, with lease expiry
simulated by moving lease timestamps into the past — the same clock advance
the product's own tests use) and a byte-level audit of everything durable.

The audit sorts each trial into exactly one outcome:

- ``landed``          terminal proof exists and its claims verify on disk.
- ``duplicated``      landed, but the daily block appears more than once.
- ``content-partial`` no terminal, yet durable content already sits in the
                      vault (daily block or session record) — nothing lost.
- ``named-failure``   no content landed, but a durable named trace exists
                      (capture_failures, task error_code, quarantine).
- ``pending-visible`` no content and no named trace, but the intent or task
                      is durably visible and still promises replay.
- ``source-only``     nothing durable exists; only the host transcript still
                      carries the content (the pre-durability window).
- ``silent-loss``     none of the above: content gone with no trace. This is
                      the only outcome that counts against the property.

What SIGKILL proves and does not prove: it tests process death at exact code
boundaries; it does not test power loss, so fsync-versus-crash claims are out
of scope here.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from durability_child import (
    KILL_EXIT_CODE,
    PRODUCER_STAGES,
    STAGE_ENV,
    STAGE_TARGETS,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
CHILD = Path(__file__).resolve().parent / "durability_child.py"
POINT_ENV = "LLMWIKI_DURABILITY_CRASH_POINT"
PAST_STAMP = "2000-01-01T00:00:00+00:00"
CHILD_TIMEOUT_SECONDS = 180
MAX_RECOVERY_ROUNDS = 4
FAKE_BODY = "- **Gotchas / debugging** — durability stand probe body"
OUTCOMES = (
    "landed",
    "duplicated",
    "content-partial",
    "named-failure",
    "pending-visible",
    "source-only",
    "silent-loss",
)
#: outcomes in which the capture's content is durably in the vault.
CONTENT_OUTCOMES = frozenset({"landed", "duplicated", "content-partial"})


@dataclass
class TrialResult:
    """Everything one trial measured, ready for aggregation."""

    stage: str | None
    point: str
    outcome: str
    recovery_runs: int
    kill_observed: bool
    worker_reasons: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)


def kill_points() -> list[tuple[str, str]]:
    """Every (stage, point) pair the stand injects, in a stable order."""
    return [(stage, point) for stage in STAGE_TARGETS for point in ("before", "after")]


def build_trial_vault(base: Path) -> tuple[Path, Path]:
    """A freshly adopted Reliability-V3 vault, exactly like the product tests."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    from installed_memory_repair import repair_installed_vault

    vault = base / "vault"
    state = base / "state"
    (vault / "knowledge" / "projects").mkdir(parents=True)
    (vault / "scripts").mkdir()
    (vault / "scripts" / "integration_adapter.py").write_bytes(
        (SCRIPTS_DIR / "integration_adapter.py").read_bytes()
    )
    report = repair_installed_vault(
        root=vault,
        state_root=state,
        adopt_ownership_v3=True,
        confirm_all_agents_stopped=True,
    )
    if report["overall_status"] != "ok":
        raise RuntimeError(f"trial vault adoption failed: {report}")
    return vault, state


def write_transcript(state: Path, marker: str) -> Path:
    """A transcript in the one state-root directory capture trusts."""
    transcripts = state / "cache" / "transient-transcripts"
    transcripts.mkdir(parents=True, exist_ok=True)
    transcript = transcripts / f"{marker}.jsonl"
    line = {"type": "user", "message": {"content": f"remember {marker}"}}
    transcript.write_text(json.dumps(line) + "\n", encoding="utf-8")
    return transcript


def child_environment(vault: Path, state: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["LLM_WIKI_ROOT"] = str(vault)
    env["LLM_WIKI_STATE_ROOT"] = str(state)
    env["MEMORY_LLM_PROVIDER"] = "fake"
    env["MEMORY_LLM_FAKE_RESPONSE"] = f"FLUSH_MINOR\n{FAKE_BODY}"
    env["PYTHONPATH"] = str(SCRIPTS_DIR)
    env.pop(STAGE_ENV, None)
    env.pop(POINT_ENV, None)
    return env


def run_child(
    mode: str,
    payload: dict,
    env: dict[str, str],
    *,
    stage: str | None = None,
    point: str = "before",
) -> subprocess.CompletedProcess:
    """One producer or worker process; `stage` arms the self-SIGKILL."""
    env = dict(env)
    if stage is not None:
        env[STAGE_ENV] = stage
        env[POINT_ENV] = point
    return subprocess.run(
        [sys.executable, str(CHILD), mode],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=CHILD_TIMEOUT_SECONDS,
    )


def _writable(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    return connection


def advance_clock(state: Path) -> dict[str, int]:
    """Move every lease deadline into the past — a simulated wait, not a repair.

    This edits only timestamps the product itself compares against wall time
    (the pattern the product's own queue tests use). It never deletes rows,
    so any state a real wait could not clear stays exactly as the crash left it.
    """
    changed: dict[str, int] = {}
    with _writable(state / "run" / "queue-v3.sqlite3") as db:
        changed["leased_tasks"] = db.execute(
            "UPDATE tasks SET lease_expires_at=? WHERE state='leased'", (PAST_STAMP,)
        ).rowcount
        changed["ready_tasks"] = db.execute(
            "UPDATE tasks SET available_at=? WHERE state='ready'", (PAST_STAMP,)
        ).rowcount
        changed["queue_owners"] = db.execute(
            "UPDATE queue_ownership SET expires_at=?", (PAST_STAMP,)
        ).rowcount
    with _writable(state / "run" / "markdown-transactions-v3.sqlite3") as db:
        changed["maintenance_owners"] = db.execute(
            "UPDATE maintenance_owners SET expires_at=?", (PAST_STAMP,)
        ).rowcount
        changed["intent_fences"] = db.execute(
            "UPDATE intent_fences SET expires_at=?",
            (PAST_STAMP.replace("+00:00", "Z"),),
        ).rowcount
    return changed


def _rows(db_path: Path, sql: str) -> list[dict]:
    if not db_path.exists():
        return []
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(sql)]


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _terminal_records(state: Path) -> list[dict]:
    results = state / "run" / "queue-results"
    if not results.exists():
        return []
    terminals = [
        path
        for path in sorted(results.glob("capture-*.json"))
        if not path.name.startswith("capture-decision-")
    ]
    return [_read_json(path) for path in terminals]


def _daily_text(vault: Path) -> str:
    daily = vault / "knowledge" / "daily"
    if not daily.exists():
        return ""
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted(daily.glob("*.md")))


def _session_record_has(vault: Path, marker: str) -> bool:
    sessions = vault / "knowledge" / "raw" / "sessions"
    if not sessions.exists():
        return False
    texts = (path.read_text(encoding="utf-8") for path in sessions.rglob("*.md"))
    return any(marker in text for text in texts)


def _output_verifies(vault: Path, output: dict) -> bool:
    path = vault / str(output.get("path", ""))
    if not path.is_file():
        return False
    return hashlib.sha256(path.read_bytes()).hexdigest() == output.get("sha256")


def _committed_outputs_verify(vault: Path, disposition: dict, daily_blocks: int) -> bool:
    """Every named output byte-verifies, or the daily block is provably present.

    The hash alternative covers a retried commit: a later append to the same
    daily file changes the whole-file hash without invalidating the block.
    """
    outputs = disposition.get("outputs") or []
    if not outputs:
        return False
    verified = all(_output_verifies(vault, output) for output in outputs)
    return verified or daily_blocks >= 1


def _terminal_verifies(vault: Path, record: dict, daily_blocks: int) -> bool:
    disposition = record.get("disposition") or {}
    kind = disposition.get("kind")
    if kind == "no_durable_content":
        return True
    if kind != "markdown_committed":
        return False
    return _committed_outputs_verify(vault, disposition, daily_blocks)


def _intent_file_count(state: Path) -> int:
    base = state / "run" / "capture-intents"
    if not base.exists():
        return 0
    return sum(1 for _ in base.rglob("*.json"))


def _failure_reasons(state: Path) -> list[str]:
    data = _read_json(state / "run" / "state.json")
    failures = data.get("capture_failures") or {}
    return sorted(
        str(entry.get("last_reason", "")) for entry in failures.values() if isinstance(entry, dict)
    )


def collect_evidence(vault: Path, state: Path, marker: str) -> dict:
    """Every durable surface the trichotomy can be decided from."""
    tasks = _rows(
        state / "run" / "queue-v3.sqlite3",
        "SELECT state,error_code,attempts FROM tasks",
    )
    intents = _rows(
        state / "run" / "queue-v3.sqlite3",
        "SELECT intent_id,publication_state FROM capture_intents",
    )
    quarantined = _rows(
        state / "run" / "markdown-transactions-v3.sqlite3",
        "SELECT state FROM \"transaction\" WHERE state LIKE 'quarantin%'",
    )
    terminals = _terminal_records(state)
    daily = _daily_text(vault)
    daily_blocks = sum(daily.count(f"Capture intent: `{row['intent_id']}`") for row in intents)
    return {
        "tasks": tasks,
        "intents": intents,
        "intent_files": _intent_file_count(state),
        "terminals": terminals,
        "terminals_verified": [
            _terminal_verifies(vault, record, daily_blocks) for record in terminals
        ],
        "daily_blocks": daily_blocks,
        "session_record": _session_record_has(vault, marker),
        "failure_reasons": _failure_reasons(state),
        "quarantined": len(quarantined),
        "transcript_present": any((state / "cache" / "transient-transcripts").glob("*.jsonl")),
    }


def _task_succeeded(evidence: dict) -> bool:
    return any(row["state"] == "succeeded" for row in evidence["tasks"])


def _landed(evidence: dict) -> bool:
    """Terminal proof on disk, verified, and the queue itself settled the task.

    A terminal file alone is not enough: a kill between the terminal-file write
    and the queue completion leaves proof of the content but an unsettled task,
    and that is content-partial, not a finished lifecycle.
    """
    verified = evidence["terminals_verified"]
    proven = bool(verified) and all(verified) and evidence["session_record"]
    return proven and _task_succeeded(evidence)


def _has_named_trace(evidence: dict) -> bool:
    named_tasks = any(row["error_code"] for row in evidence["tasks"])
    return bool(evidence["failure_reasons"]) or named_tasks or evidence["quarantined"] > 0


def _is_visible(evidence: dict) -> bool:
    replayable = any(row["state"] in ("ready", "leased") for row in evidence["tasks"])
    return replayable or bool(evidence["intents"]) or evidence["intent_files"] > 0


def _classify_landed(evidence: dict) -> str:
    if evidence["daily_blocks"] > 1:
        return "duplicated"
    return "landed"


def _has_content(evidence: dict) -> bool:
    return evidence["daily_blocks"] >= 1 or evidence["session_record"]


def _has_source(evidence: dict) -> bool:
    return bool(evidence["transcript_present"])


#: unlanded verdicts, strongest evidence first; falling through all of them
#: means content gone with no trace — the silent loss the stand exists to find.
_UNLANDED_RULES = (
    (_has_content, "content-partial"),
    (_has_named_trace, "named-failure"),
    (_is_visible, "pending-visible"),
    (_has_source, "source-only"),
)


def _classify_unlanded(evidence: dict) -> str:
    for predicate, outcome in _UNLANDED_RULES:
        if predicate(evidence):
            return outcome
    return "silent-loss"


def classify(evidence: dict) -> str:
    """The trichotomy verdict for one trial's final durable state."""
    if _landed(evidence):
        return _classify_landed(evidence)
    return _classify_unlanded(evidence)


def _settled(outcome: str) -> bool:
    """Only a finished lifecycle stops recovery; partial content must keep trying."""
    return outcome in ("landed", "duplicated")


def _recovery_rounds(
    vault: Path, state: Path, marker: str, env: dict[str, str]
) -> tuple[int, list[str]]:
    """The documented recovery: expire leases, run the worker, re-audit."""
    reasons: list[str] = []
    for round_index in range(MAX_RECOVERY_ROUNDS):
        if _settled(classify(collect_evidence(vault, state, marker))):
            return round_index, reasons
        advance_clock(state)
        run_child("work", {}, env)
        reasons = _failure_reasons(state)
    return MAX_RECOVERY_ROUNDS, reasons


@dataclass(frozen=True)
class TrialSpec:
    """One trial's kill assignment; stage None runs the pipeline unharmed."""

    stage: str | None
    point: str


def _died(returncode: int) -> bool:
    """Whether a child took the armed kill, in either host's spelling.

    POSIX reports a SIGKILL as -9. Windows has no such signal, so the child
    leaves through `os._exit(KILL_EXIT_CODE)` instead — weaker evidence, and
    `durability_child._die` says why.
    """
    return returncode in {-9, KILL_EXIT_CODE}


def _initial_runs(spec: TrialSpec, payload: dict, env: dict[str, str]) -> tuple[bool, int]:
    """Producer then worker, with the kill armed in the owning process.

    Returns (kill observed, recovery runs already spent). After a producer-side
    kill, the immediately following clean worker run is itself the first
    recovery action — in the real system it happens at the next session.
    """
    producer_stage = spec.stage if spec.stage in PRODUCER_STAGES else None
    worker_stage = spec.stage if producer_stage is None else None
    produced = run_child("produce", payload, env, stage=producer_stage, point=spec.point)
    worked = run_child("work", payload, env, stage=worker_stage, point=spec.point)
    killed = _died(produced.returncode) or _died(worked.returncode)
    return killed, int(producer_stage is not None)


def run_trial(spec: TrialSpec, base: Path, marker: str) -> TrialResult:
    """One isolated vault, one capture, one kill, recovery, final audit."""
    base.mkdir(parents=True)
    vault, state = build_trial_vault(base)
    transcript = write_transcript(state, marker)
    payload = {"session_id": marker, "transcript_path": str(transcript)}
    env = child_environment(vault, state)
    kill_observed, initial_recovery = _initial_runs(spec, payload, env)
    ladder_runs, reasons = _recovery_rounds(vault, state, marker, env)
    evidence = collect_evidence(vault, state, marker)
    return TrialResult(
        stage=spec.stage,
        point=spec.point,
        outcome=classify(evidence),
        recovery_runs=initial_recovery + ladder_runs,
        kill_observed=kill_observed,
        worker_reasons=reasons,
        evidence=evidence,
    )


def discard_trial(base: Path) -> None:
    shutil.rmtree(base, ignore_errors=True)
