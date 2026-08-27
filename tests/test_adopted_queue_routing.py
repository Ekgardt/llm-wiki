"""The queue's half of the adoption rule.

Adoption replaces `run/queue.sqlite3` with a JSON tombstone. The coordinator
already has one rule that decides which database a writer gets; the queue had
none, so every reader constructed `MemoryQueue` directly and opened the
tombstone. These tests build a genuinely adopted state root with the real
adoption command and then ask the operator paths to work on it.
"""

from __future__ import annotations

import ast
import json
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import memory_queue  # noqa: E402
from installed_memory_repair import repair_installed_vault  # noqa: E402

_ADOPTION_TOMBSTONE_SCHEMA = "operational-db-tombstone/v1"


def _adopted_vault(tmp_path: Path) -> tuple[Path, Path]:
    """A vault whose queue and coordinator went through real V3 adoption."""
    root = tmp_path / "vault"
    state_root = tmp_path / "state"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts/integration_adapter.py").write_bytes(
        (SCRIPTS_DIR / "integration_adapter.py").read_bytes()
    )
    report = repair_installed_vault(
        root=root,
        state_root=state_root,
        adopt_ownership_v3=True,
        confirm_all_agents_stopped=True,
    )
    assert report["overall_status"] == "ok"
    tombstone = json.loads((state_root / "run/queue.sqlite3").read_text())
    assert tombstone["schema_version"] == _ADOPTION_TOMBSTONE_SCHEMA
    return root, state_root


def _unadopted_vault(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "plain-vault"
    state_root = tmp_path / "plain-state"
    (root / "scripts").mkdir(parents=True)
    state_root.mkdir(parents=True)
    return root, state_root


def _point_process_at(monkeypatch, root: Path, state_root: Path) -> None:
    monkeypatch.setenv("LLM_WIKI_ROOT", str(root))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))


def test_the_rule_hands_an_adopted_vault_the_adopted_queue(tmp_path: Path) -> None:
    root, state_root = _adopted_vault(tmp_path)

    queue = memory_queue.active_or_legacy_memory_queue(root, state_root)

    assert queue.db_path == state_root / "run/queue-v3.sqlite3"
    assert queue.run_dir == state_root / "run"


def test_the_rule_hands_an_unadopted_vault_the_legacy_queue(tmp_path: Path) -> None:
    root, state_root = _unadopted_vault(tmp_path)

    queue = memory_queue.active_or_legacy_memory_queue(root, state_root)

    assert isinstance(queue, memory_queue.MemoryQueue)
    assert queue.db_path == state_root / "run/queue.sqlite3"


def test_operator_status_reads_an_adopted_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The measured defect: `status` answered `sqlite_error` on this vault."""
    root, state_root = _adopted_vault(tmp_path)
    _point_process_at(monkeypatch, root, state_root)
    memory_queue.active_or_legacy_memory_queue(root, state_root).enqueue(
        "query", 1, {"prompt": "status"}
    )

    status = memory_queue._operator_status()

    assert status["counts"] == {"total": 1}
    assert status["states"]["ready"] == 1


def test_the_worker_entry_point_reads_an_adopted_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Step one of the nightly pass: `work` refused with `sqlite_error`."""
    root, state_root = _adopted_vault(tmp_path)
    _point_process_at(monkeypatch, root, state_root)
    memory_queue.active_or_legacy_memory_queue(root, state_root).enqueue(
        "query", 1, {"prompt": "work"}
    )

    summary = memory_queue.run_worker(
        lambda _task: True,
        max_tasks=1,
        idle_seconds=0,
        processor_runner=memory_queue._run_processor_inline,
    )

    assert summary.processed == 1
    assert summary.succeeded == 1
    assert summary.remaining_eligible == 0


def test_the_adopted_queue_answers_get_and_list(tmp_path: Path) -> None:
    root, state_root = _adopted_vault(tmp_path)
    queue = memory_queue.active_or_legacy_memory_queue(root, state_root)
    task_id = queue.enqueue("query", 1, {"prompt": "listed"}, priority=7)

    listed = queue.list_tasks(states=("ready",))
    fetched = queue.get(task_id)

    assert [task.id for task in listed] == [task_id]
    assert (fetched.payload, fetched.priority) == ({"prompt": "listed"}, 7)


def test_the_adopted_queue_counts_eligible_work(tmp_path: Path) -> None:
    root, state_root = _adopted_vault(tmp_path)
    queue = memory_queue.active_or_legacy_memory_queue(root, state_root)
    queue.enqueue("query", 1, {"prompt": "counted"})

    assert queue.count_eligible() == 1
    assert queue.retains_run_directory() is True


def test_compile_source_failures_survive_adoption(tmp_path: Path) -> None:
    root, state_root = _adopted_vault(tmp_path)
    queue = memory_queue.active_or_legacy_memory_queue(root, state_root)
    logical_path = "knowledge/daily/2026-08-26.md"
    digest = "a" * 64

    queue.record_source_failure(
        logical_path, digest, error_code="ValueError", producer="compile"
    )
    recorded = queue.source_failure(logical_path, digest)
    queue.clear_source_failure(logical_path, digest)

    assert recorded is not None
    assert recorded["error_code"] == "ValueError"
    assert queue.source_failure(logical_path, digest) is None


def test_a_direct_legacy_construction_names_the_tombstone(tmp_path: Path) -> None:
    """NEW-104: `file is not a database` named neither boundary nor path."""
    _root, state_root = _adopted_vault(tmp_path)

    with pytest.raises(memory_queue.QueueOperationError) as raised:
        memory_queue.MemoryQueue(state_root)

    assert raised.value.code == "queue_tombstoned_by_adoption"
    assert "run/queue.sqlite3" in raised.value.detail.replace("\\", "/")
    assert "run/queue-v3.sqlite3" in raised.value.detail.replace("\\", "/")


def test_the_cli_refusal_prints_the_tombstoned_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _root, state_root = _adopted_vault(tmp_path)

    with pytest.raises(memory_queue.QueueOperationError) as raised:
        memory_queue.MemoryQueue(state_root)
    assert memory_queue._emit_cli_error(raised.value) == 2

    printed = json.loads(capsys.readouterr().out)
    assert printed["codes"] == ["queue_tombstoned_by_adoption"]
    assert "queue-v3.sqlite3" in printed["detail"]
    # The detail is composed with state-root-relative paths so the CLI's
    # 240-character bound cannot cut the adopted path out of the refusal on a
    # deep macOS/Windows temp root. Ending intact proves nothing was cut.
    assert printed["detail"].endswith("active_or_legacy_memory_queue()")


def test_the_source_fence_family_works_on_the_adopted_queue(tmp_path: Path) -> None:
    """The 2026-08-27 gap is closed: the fence family has a V3 implementation.

    Until then this family was refused by name (`queue_api_not_adopted`).
    Its full contract is proved in `test_adopted_source_fence_and_owner.py`;
    this test keeps the routing file's own claim honest.
    """
    root, state_root = _adopted_vault(tmp_path)
    queue = memory_queue.active_or_legacy_memory_queue(root, state_root)

    fence = queue.acquire_source_fence("2026-08-26", "b" * 64)

    with sqlite3.connect(state_root / "run/queue-v3.sqlite3") as database:
        stored = database.execute(
            "SELECT logical_path, token FROM source_fences"
        ).fetchall()
    assert stored == [("knowledge/daily/2026-08-26.md", fence.token)]
    queue.release_source_fence(fence.token)


def _direct_constructions(path: Path) -> list[int]:
    """Every line that builds a `MemoryQueue` outside the rule itself."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        called = isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        if called and node.func.id == "MemoryQueue":
            found.append(node.lineno)
    return found


def test_no_reader_opens_the_queue_outside_the_rule() -> None:
    outside = {
        name: _direct_constructions(SCRIPTS_DIR / name)
        for name in ("doctor.py", "archive_daily.py", "compile_memory.py", "mcp_server.py")
    }

    assert outside == {
        "doctor.py": [],
        "archive_daily.py": [],
        "compile_memory.py": [],
        "mcp_server.py": [],
    }


def test_a_legacy_queue_on_a_real_database_still_opens(tmp_path: Path) -> None:
    """The refusal must read a tombstone, never an ordinary queue database."""
    root, state_root = _unadopted_vault(tmp_path)
    memory_queue.active_or_legacy_memory_queue(root, state_root).enqueue(
        "query", 1, {"prompt": "legacy"}
    )

    reopened = memory_queue.MemoryQueue(state_root)

    assert reopened.count_eligible() == 1
    with sqlite3.connect(state_root / "run/queue.sqlite3") as database:
        assert database.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
