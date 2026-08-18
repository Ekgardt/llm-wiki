from __future__ import annotations

import concurrent.futures
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import blackboard
import pytest

from tests.test_reliability_v3_adoption import (
    _vault,
    build_adopted_reliability_v3,
)


def _under_contention(call, *arguments, attempts: int = 6, **keywords):
    """Retry a blackboard call whose global writer gate timed out.

    Every blackboard operation, reads included, appends through the one global
    Markdown writer gate, and each append hardens files with `icacls` on
    Windows. With six processes on a hosted runner a caller can lose that gate
    for longer than its ten-second budget. That is contention, not incoherence,
    and a real caller retries it.
    """
    for attempt in range(attempts):
        try:
            return call(*arguments, **keywords)
        except TimeoutError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))
    raise AssertionError("unreachable")


def _write_blackboard_batch(vault: str, state_root: str, worker: int, count: int) -> int:
    os.environ["LLM_WIKI_ROOT"] = vault
    os.environ["LLM_WIKI_STATE_ROOT"] = state_root
    blackboard.PROJECTS_DIR = Path(vault) / "knowledge/projects"
    completed = 0
    for index in range(count):
        claim = _under_contention(
            blackboard.claim_task,
            "demo",
            f"worker {worker} task {index}",
            f"agent-{worker}",
            resources=[f"worker/{worker}/task/{index}"],
        )
        if _under_contention(blackboard.complete_task, "demo", claim):
            completed += 1
    return completed


def _read_blackboard_status(vault: str, state_root: str, count: int) -> int:
    os.environ["LLM_WIKI_ROOT"] = vault
    os.environ["LLM_WIKI_STATE_ROOT"] = state_root
    blackboard.PROJECTS_DIR = Path(vault) / "knowledge/projects"
    largest = 0
    for _ in range(count):
        status = _under_contention(blackboard.get_status, "demo")
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
    first = blackboard.claim_task(
        "demo",
        "own auth",
        "opencode",
        resources=["src/Auth.py", "docs/auth.md"],
    )

    with pytest.raises(blackboard.BlackboardConflictError) as raised:
        blackboard.claim_task(
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
    first = blackboard.claim_task(
        "demo",
        "own graph",
        "opencode",
        resources=["src/graph.py"],
        ttl_seconds=30,
        now=now,
    )
    renewed = blackboard.heartbeat_claim(first, now=now + timedelta(seconds=20))

    with pytest.raises(blackboard.BlackboardConflictError):
        blackboard.claim_task(
            "demo",
            "competing graph",
            "codex",
            resources=["src/graph.py"],
            now=now + timedelta(seconds=40),
        )

    successor = blackboard.claim_task(
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
    blackboard.claim_task(
        "demo", "own queue", "opencode", resources=["scripts/memory_queue.py"]
    )
    with pytest.raises(blackboard.BlackboardConflictError) as raised:
        blackboard.claim_task(
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
