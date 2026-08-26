from __future__ import annotations

import concurrent.futures
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import blackboard
import markdown_transaction
import pytest

from tests.test_reliability_v3_adoption import (
    _vault,
    build_adopted_reliability_v3,
)


def _child_writer_budget() -> None:
    """Give a child the gate budget a hosted Windows image actually needs.

    The SQLite claim is durable before the journal line is appended, so a
    caller that loses the gate in between holds resources it was never told
    about. `_publish_active_claim` releases them, and since 2026-08-22 it keeps
    trying rather than failing quietly, which is what makes the retry converge.
    Waiting long enough for the gate keeps these processes from needing that
    path at all.
    """
    markdown_transaction._WRITER_WAIT_SECONDS = 120.0


def _is_contention(error: BaseException) -> bool:
    """Losing the writer gate, its lease, or the SQLite lock is contention."""
    if isinstance(error, TimeoutError):
        return True
    if isinstance(error, sqlite3.OperationalError):
        return "database is locked" in str(error)
    return isinstance(error, RuntimeError) and "gate ownership was lost" in str(error)


def _under_contention(call, *arguments, attempts: int = 12, label: str = "", **keywords):
    """Retry a blackboard call whose global writer gate timed out.

    Every blackboard operation, reads included, appends through the one global
    Markdown writer gate, and each append hardens files with `icacls` on
    Windows. With six processes on a hosted runner a caller can lose that gate
    for longer than its ten-second budget. That is contention, not incoherence,
    and a real caller retries it.

    What finally escapes carries the whole history. A conflict is not
    contention, so it is raised on the spot — and on 2026-08-22 and 2026-08-26
    that reached CI as one sentence naming no resource, no attempt and no
    elapsed time, while the contention that caused it had already been
    swallowed here. The swallowed errors are the cause; they travel with it.
    """
    swallowed: list[str] = []
    started = time.monotonic()
    for attempt in range(attempts):
        try:
            return call(*arguments, **keywords)
        except Exception as error:
            _swallow_or_report(error, swallowed, attempt, attempts, label, started)
            time.sleep(0.05 * (attempt + 1))
    raise AssertionError("unreachable")


def _swallow_or_report(
    error: Exception,
    swallowed: list[str],
    attempt: int,
    attempts: int,
    label: str,
    started: float,
) -> None:
    """Keep retrying contention, or report the failure with what led to it."""
    swallowed.append(f"attempt {attempt + 1}: {error!r}")
    if _is_contention(error) and attempt < attempts - 1:
        return
    raise AssertionError(
        f"{label or 'blackboard call'} failed after {attempt + 1} of "
        f"{attempts} attempt(s) in {time.monotonic() - started:.2f}s; "
        + " | ".join(swallowed)
    ) from error


def _write_blackboard_batch(vault: str, state_root: str, worker: int, count: int) -> int:
    os.environ["LLM_WIKI_ROOT"] = vault
    os.environ["LLM_WIKI_STATE_ROOT"] = state_root
    blackboard.PROJECTS_DIR = Path(vault) / "knowledge/projects"
    _child_writer_budget()
    completed = 0
    for index in range(count):
        claim = _under_contention(
            blackboard.claim_task,
            "demo",
            f"worker {worker} task {index}",
            f"agent-{worker}",
            label=f"claim of worker/{worker}/task/{index}",
            resources=[f"worker/{worker}/task/{index}"],
            # The default lease is thirty seconds. Six processes queueing for
            # one writer gate on a hosted Windows image can spend longer than
            # that between claim and completion, and the expiry then fails the
            # run for contention rather than for incoherence.
            ttl_seconds=600,
        )
        if _under_contention(
            blackboard.complete_task,
            "demo",
            claim,
            label=f"completion of worker/{worker}/task/{index}",
        ):
            completed += 1
    return completed


# The default claim lease is thirty seconds. Most tests here are about what a
# claim records, not about when it expires, and a loaded hosted runner can spend
# longer than that between claiming and completing — measured on CI 2026-08-24,
# where `test_a_claim_whose_record_landed_before_the_failure_stands` failed with
# "blackboard claim lease expired" on Windows. The multiprocess helper above
# already asks for a longer lease for exactly this reason.
_LONG_TTL_SECONDS = 900


def _claim(*arguments, **keywords):
    """Claim a task with a lease long enough that expiry is not the subject."""
    keywords.setdefault("ttl_seconds", _LONG_TTL_SECONDS)
    return blackboard.claim_task(*arguments, **keywords)


def _read_blackboard_status(vault: str, state_root: str, count: int) -> int:
    os.environ["LLM_WIKI_ROOT"] = vault
    os.environ["LLM_WIKI_STATE_ROOT"] = state_root
    blackboard.PROJECTS_DIR = Path(vault) / "knowledge/projects"
    _child_writer_budget()
    largest = 0
    for _ in range(count):
        status = _under_contention(
            blackboard.get_status, "demo", label="status read"
        )
        largest = max(largest, status["active_tasks"] + status["completed_tasks"])
        assert status["active_tasks"] >= 0
        assert status["completed_tasks"] >= 0
        time.sleep(0.002)
    return largest


def _compete_for_blackboard_resource(
    vault: str,
    state_root: str,
    agent: str,
    start_at: float,
) -> tuple[str, str]:
    os.environ["LLM_WIKI_ROOT"] = vault
    os.environ["LLM_WIKI_STATE_ROOT"] = state_root
    blackboard.PROJECTS_DIR = Path(vault) / "knowledge/projects"
    _child_writer_budget()
    time.sleep(max(0.0, start_at - time.monotonic()))
    try:
        claim = blackboard.claim_task(
            "demo",
            f"{agent} owns shared resource",
            agent,
            resources=["src/shared.py"],
        )
    except blackboard.BlackboardConflictError as exc:
        return "conflict", exc.conflict_id
    return "claimed", claim.claim_id


@pytest.fixture
def blackboard_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    vault, state_root = _vault(tmp_path)
    build_adopted_reliability_v3(vault, state_root)
    projects = vault / "knowledge/projects"
    (projects / "demo/.blackboard").mkdir(parents=True)
    monkeypatch.setattr(blackboard, "PROJECTS_DIR", projects)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))
    return vault, state_root


def test_status_rejects_malformed_blackboard_stream(
    blackboard_vault: tuple[Path, Path],
) -> None:
    vault, _state_root = blackboard_vault
    tasks = vault / "knowledge/projects/demo/.blackboard/tasks.jsonl"
    tasks.write_text('{"id":"task-1"}\nnot-json\n', encoding="utf-8")

    with pytest.raises(ValueError, match="tasks.jsonl line 2 is corrupt"):
        blackboard.get_status("demo")


def test_parser_rejects_stream_over_shared_transaction_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(blackboard, "MAX_KNOWLEDGE_TARGET_BYTES", 8)

    with pytest.raises(ValueError, match="tasks.jsonl exceeds the blackboard stream limit"):
        blackboard._parse_jsonl(Path("tasks.jsonl"), b'{"id":1}\n')


def test_status_reads_all_streams_from_one_coherent_snapshot(
    blackboard_vault: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, state_root = blackboard_vault
    observed: list[tuple[Path, ...]] = []
    task = {
        "id": "task-1",
        "task": "bounded status",
        "agent": "opencode",
        "status": "claimed",
    }

    class Coordinator:
        def __init__(self, actual_vault: Path, actual_state_root: Path):
            assert actual_vault == vault
            assert actual_state_root == state_root

        def coherent_read(self, paths):
            paths = tuple(paths)
            observed.append(paths)
            content = {
                "tasks.jsonl": json.dumps(task).encode() + b"\n",
                "completed.jsonl": b"",
                "signals.jsonl": b"",
            }
            return {path: content[path.name] for path in paths}

    monkeypatch.setattr(
        blackboard,
        "_coordinator",
        lambda: Coordinator(vault, state_root),
    )

    status = blackboard.get_status("demo")

    assert len(observed) == 1
    assert {path.name for path in observed[0]} == {
        "tasks.jsonl",
        "completed.jsonl",
        "signals.jsonl",
    }
    assert status["active_tasks"] == 1
    assert status["tasks"] == [task]


def test_multiprocess_status_reads_remain_coherent_during_claim_and_complete(
    blackboard_vault: tuple[Path, Path],
) -> None:
    vault, state_root = blackboard_vault
    writers = 4
    tasks_per_writer = 6

    with concurrent.futures.ProcessPoolExecutor(max_workers=6) as executor:
        readers = [
            executor.submit(
                _read_blackboard_status, str(vault), str(state_root), 80
            )
            for _ in range(2)
        ]
        writes = [
            executor.submit(
                _write_blackboard_batch,
                str(vault),
                str(state_root),
                worker,
                tasks_per_writer,
            )
            for worker in range(writers)
        ]
        assert [future.result(timeout=300) for future in writes] == [
            tasks_per_writer
        ] * writers
        assert all(future.result(timeout=300) >= 0 for future in readers)

    status = blackboard.get_status("demo")
    assert status["active_tasks"] == 0
    assert status["completed_tasks"] == writers * tasks_per_writer


def test_multiprocess_same_resource_claim_has_one_fenced_winner(
    blackboard_vault: tuple[Path, Path],
) -> None:
    vault, state_root = blackboard_vault
    start_at = time.monotonic() + 2.0

    with concurrent.futures.ProcessPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                _compete_for_blackboard_resource,
                str(vault),
                str(state_root),
                agent,
                start_at,
            )
            for agent in ("opencode", "codex")
        ]
        results = [future.result(timeout=300) for future in futures]

    assert sorted(status for status, _identity in results) == ["claimed", "conflict"]
    with sqlite3.connect(
        state_root / "run/markdown-transactions-v3.sqlite3"
    ) as database:
        rows = database.execute(
            "SELECT resource,claim_id FROM blackboard_claims"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "src/shared.py"
    assert rows[0][1] in {identity for _status, identity in results}


def test_claim_is_all_or_none_when_one_resource_is_busy(
    blackboard_vault: tuple[Path, Path],
) -> None:
    _vault_root, state_root = blackboard_vault
    first = _claim(
        "demo",
        "own auth",
        "opencode",
        resources=["src/Auth.py", "docs/auth.md"],
    )

    with pytest.raises(blackboard.BlackboardConflictError) as raised:
        _claim(
            "demo",
            "change auth and catalog",
            "codex",
            resources=["SRC\\auth.py", "src/catalog.py"],
        )

    assert raised.value.resources == ("src/auth.py",)
    with sqlite3.connect(
        state_root / "run/markdown-transactions-v3.sqlite3"
    ) as database:
        rows = database.execute(
            "SELECT resource,claim_id FROM blackboard_claims ORDER BY resource"
        ).fetchall()
    assert rows == [
        ("docs/auth.md", first.claim_id),
        ("src/auth.py", first.claim_id),
    ]


def test_heartbeat_expiry_reclaim_and_stale_fencing(
    blackboard_vault: tuple[Path, Path],
) -> None:
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    first = _claim(
        "demo",
        "own graph",
        "opencode",
        resources=["src/graph.py"],
        ttl_seconds=30,
        now=now,
    )
    renewed = blackboard.heartbeat_claim(first, now=now + timedelta(seconds=20))

    with pytest.raises(blackboard.BlackboardConflictError):
        _claim(
            "demo",
            "competing graph",
            "codex",
            resources=["src/graph.py"],
            now=now + timedelta(seconds=40),
        )

    successor = _claim(
        "demo",
        "reclaimed graph",
        "codex",
        resources=["src/graph.py"],
        now=now + timedelta(seconds=51),
    )
    assert successor.resource_epochs[0][1] > renewed.resource_epochs[0][1]
    with pytest.raises(blackboard.BlackboardFenceError):
        blackboard.heartbeat_claim(renewed, now=now + timedelta(seconds=52))


def test_conflict_and_resolution_are_immutable_events(
    blackboard_vault: tuple[Path, Path],
) -> None:
    vault, _state_root = blackboard_vault
    _claim(
        "demo", "own queue", "opencode", resources=["scripts/memory_queue.py"]
    )
    with pytest.raises(blackboard.BlackboardConflictError) as raised:
        _claim(
            "demo", "also own queue", "codex", resources=["scripts/memory_queue.py"]
        )

    conflicts = blackboard.detect_conflicts("demo")
    assert [item["conflict_id"] for item in conflicts] == [raised.value.conflict_id]
    blackboard.resolve_conflict(
        "demo",
        raised.value.conflict_id,
        agent="operator",
        resolution="codex waits for opencode",
    )

    assert blackboard.detect_conflicts("demo") == []
    records = blackboard._read_jsonl(
        vault / "knowledge/projects/demo/.blackboard/conflicts.jsonl"
    )
    assert [record["kind"] for record in records] == ["conflict", "resolution"]


def test_a_retried_completion_keeps_the_first_published_record(blackboard_vault):
    """A completion is published once; a retry must not re-stamp or be refused."""
    claim = _claim(
        "demo", "shared task", "agent-a", resources=["src/one.py"]
    )
    published = blackboard._completion_record(claim, blackboard._utc_now(None))
    completed_file = blackboard._bb_dir("demo") / "completed.jsonl"
    blackboard._append_jsonl(
        completed_file, published, f"blackboard-complete:{claim.claim_id}"
    )

    time.sleep(0.01)
    assert blackboard.complete_task("demo", claim) is True

    records = blackboard._read_jsonl(completed_file)
    assert [record["id"] for record in records] == [claim.claim_id]
    assert records[0]["completed_at"] == published["completed_at"]
    assert blackboard.get_status("demo")["active_tasks"] == 0


def test_a_reconciled_activation_does_not_break_the_claimer(blackboard_vault):
    """The reader may publish the activation first; the claimer must still finish."""
    claim = blackboard._new_claim(
        "demo",
        "shared task",
        "agent-a",
        blackboard._normalize_resources(["src/one.py", "src/two.py"]),
        30,
        blackboard._utc_now(None),
    )
    tasks_file = blackboard._bb_dir("demo") / "tasks.jsonl"
    blackboard._append_jsonl(
        tasks_file,
        blackboard._claim_request_record(claim),
        f"blackboard-request:{claim.claim_id}",
    )
    acquired = blackboard._acquire_claim(blackboard._coordinator(), claim)

    blackboard.get_status("demo")

    blackboard._append_jsonl(
        tasks_file,
        blackboard._claim_active_record(acquired),
        f"blackboard-active:{claim.claim_id}",
    )
    assert blackboard.get_status("demo")["active_tasks"] == 1


def test_a_claim_whose_record_fails_releases_its_resources(
    blackboard_vault: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller that never learns it holds a claim must not be blocked by it."""
    real_append = blackboard._append_jsonl
    failures: list[str] = []

    def failing_append(path, record, operation_id):
        if operation_id.startswith("blackboard-active:"):
            failures.append(operation_id)
            raise TimeoutError("Markdown writer gate deadline expired")
        return real_append(path, record, operation_id)

    monkeypatch.setattr(blackboard, "_append_jsonl", failing_append)
    with pytest.raises(TimeoutError):
        _claim(
            "demo", "task", "agent-a", resources=["shared/resource"]
        )
    assert len(failures) == 1

    monkeypatch.setattr(blackboard, "_append_jsonl", real_append)
    claim = _claim(
        "demo", "task", "agent-a", resources=["shared/resource"]
    )
    assert claim.resources == ("shared/resource",)
    assert blackboard.complete_task("demo", claim) is True


def test_a_claim_whose_record_landed_before_the_failure_stands(
    blackboard_vault: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An append that commits and then fails must not orphan its activation."""
    real_append = blackboard._append_jsonl
    failures: list[str] = []

    def append_then_fail(path, record, operation_id):
        real_append(path, record, operation_id)
        if operation_id.startswith("blackboard-active:"):
            failures.append(operation_id)
            raise TimeoutError("Markdown writer gate deadline expired")

    monkeypatch.setattr(blackboard, "_append_jsonl", append_then_fail)
    claim = _claim(
        "demo", "task", "agent-a", resources=["shared/resource"]
    )
    assert len(failures) == 1

    monkeypatch.setattr(blackboard, "_append_jsonl", real_append)
    status = blackboard.get_status("demo")
    assert status["active_tasks"] == 1
    assert blackboard.complete_task("demo", claim) is True
    assert blackboard.get_status("demo")["active_tasks"] == 0


def test_a_failed_call_reports_the_contention_that_led_to_it() -> None:
    """What escapes the retry names the resource, the attempts and the cause.

    The conflict this test injects is what CI reported twice; on its own it is
    one sentence with no history, and the contention that produced the leak has
    already been swallowed by the retry above.
    """
    calls: list[int] = []

    def conflicted():
        calls.append(1)
        if len(calls) < 3:
            raise TimeoutError("Markdown writer gate deadline expired")
        raise blackboard.BlackboardConflictError(("worker/1/task/0",), "conflict-1")

    with pytest.raises(AssertionError) as raised:
        _under_contention(conflicted, label="claim of worker/1/task/0")

    message = str(raised.value)
    for expected in (
        "claim of worker/1/task/0",
        "3 of 12 attempt(s)",
        "attempt 1: TimeoutError",
        "attempt 2: TimeoutError",
        "BlackboardConflictError",
    ):
        assert expected in message, message
    assert isinstance(raised.value.__cause__, blackboard.BlackboardConflictError)
