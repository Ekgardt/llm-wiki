"""Operator CLI tests for memory_queue: list, status, cancel, redrive, purge and quarantine output,
the work command's defaults and overrides, canonical failure codes and the state-free process probe.

The bounded worker itself is tested in `tests/test_memory_queue_worker.py`.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import memory_queue  # noqa: E402
import queue_core  # noqa: E402
from queue_core import (  # noqa: E402
    MemoryQueue,
)


def test_cli_list_outputs_only_operator_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))
    MemoryQueue(tmp_path)
    task_id = queue_core.enqueue("query", {"prompt": "do not print"})
    monkeypatch.setattr(sys, "argv", ["memory_queue.py", "list"])

    assert memory_queue.cli() == 0

    output = capsys.readouterr().out
    record = json.loads(output)
    assert record == [{"id": task_id, "state": "ready"}]
    assert "prompt" not in output and str(tmp_path) not in output


def test_cli_status_outputs_counts_states_capabilities_and_codes_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))
    MemoryQueue(tmp_path)
    monkeypatch.setattr(sys, "argv", ["memory_queue.py", "status"])

    assert memory_queue.cli() == 0

    output = json.loads(capsys.readouterr().out)
    assert set(output) == {"counts", "states", "capabilities", "codes"}
    assert str(tmp_path) not in json.dumps(output)


def test_cli_cancel_redrive_and_purge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))
    queue = MemoryQueue(tmp_path)
    task_id = queue.enqueue("query", 1, {})
    monkeypatch.setattr(sys, "argv", ["memory_queue.py", "cancel", task_id])
    assert memory_queue.cli() == 0
    assert json.loads(capsys.readouterr().out) == {"id": task_id, "state": "cancelled"}

    old = queue.enqueue("query", 1, {})
    lease = queue.claim("worker")
    assert lease is not None and lease.id == old
    queue.fail(lease, queue_core.QueueFailure("invalid_input", permanent=True))
    monkeypatch.setattr(sys, "argv", ["memory_queue.py", "redrive", old])
    assert memory_queue.cli() == 0
    redriven = json.loads(capsys.readouterr().out)
    assert set(redriven) == {"id", "state"}
    assert redriven["state"] == "ready"

    before = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    export_parent = tmp_path / "exports"
    export_parent.mkdir()
    queue_core.harden_owner_only(export_parent, 0o700)
    export = export_parent / "export"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "memory_queue.py",
            "purge",
            "--terminal-before",
            before,
            "--export",
            str(export),
        ],
    )
    assert memory_queue.cli() == 0
    assert set(json.loads(capsys.readouterr().out)) == {"counts", "ids", "retained"}


@pytest.mark.parametrize("reason", ["", "x" * 4097])
def test_cli_quarantine_rejects_reason_before_database_work(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    reason: str,
) -> None:
    monkeypatch.setattr(
        queue_core,
        "v3_queue_for_cli",
        lambda: pytest.fail("database must not open for an invalid reason"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["memory_queue.py", "quarantine-corrupt", "task-secret", "--reason", reason],
    )

    assert memory_queue.cli() == 2
    output = capsys.readouterr().out
    assert output == '{"codes":["invalid_input"]}\n'
    if reason:
        assert reason not in output


def test_cli_quarantine_outputs_only_stable_progress_fields(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Queue:
        def quarantine_corrupt(self, task_id, *, reason, owner):
            assert (task_id, reason, owner) == ("requested-task", "retain", "owner")
            return queue_core.CorruptExportProgress(
                task_id=task_id,
                operation_id="corrupt-export:" + "a" * 64,
                state="quarantine_pending",
                pages_written=2,
                links_exported=1500,
                complete=False,
                code="capture_link_conflicted",
            )

    monkeypatch.setattr(queue_core, "v3_queue_for_cli", Queue)
    monkeypatch.setattr(queue_core, "repair_owner_for_cli", lambda _queue: nullcontext("owner"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "memory_queue.py",
            "quarantine-corrupt",
            "requested-task",
            "--reason",
            "retain",
        ],
    )

    assert memory_queue.cli() == 0
    assert json.loads(capsys.readouterr().out) == {
        "code": "capture_link_conflicted",
        "complete": False,
        "operation_id": "corrupt-export:" + "a" * 64,
        "page_count": 2,
        "state": "quarantine_pending",
        "task_id": "requested-task",
    }


def test_cli_purge_corrupt_outputs_only_stable_progress_fields(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Queue:
        def purge_quarantined(self, task_id, *, owner):
            assert (task_id, owner) == ("requested-task", "owner")
            return queue_core.CorruptPurgeProgress(
                task_id=task_id,
                operation_id="corrupt-purge:" + "b" * 64,
                state="purge_pending",
                pages_written=3,
                links_deleted=2500,
                complete=False,
                code="corrupt_child_retention_active",
            )

    monkeypatch.setattr(queue_core, "v3_queue_for_cli", Queue)
    monkeypatch.setattr(queue_core, "repair_owner_for_cli", lambda _queue: nullcontext("owner"))
    monkeypatch.setattr(
        sys,
        "argv",
        ["memory_queue.py", "purge-corrupt", "requested-task"],
    )

    assert memory_queue.cli() == 0
    assert json.loads(capsys.readouterr().out) == {
        "code": "corrupt_child_retention_active",
        "complete": False,
        "links_deleted": 2500,
        "operation_id": "corrupt-purge:" + "b" * 64,
        "page_count": 3,
        "state": "purge_pending",
        "task_id": "requested-task",
    }


def test_cli_work_uses_operational_defaults(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, object] = {}

    def worker(processor, **kwargs):
        seen.update(kwargs)
        return queue_core.WorkerSummary(0, 0, 0, 0, 0)

    monkeypatch.setattr(queue_core, "run_worker", worker)
    monkeypatch.setattr(sys, "argv", ["memory_queue.py", "work"])

    assert memory_queue.cli() == 0
    assert seen == {
        "max_tasks": 20,
        "max_seconds": 600,
        "idle_seconds": 2,
        "lease_seconds": 120,
        "heartbeat_seconds": 40,
        "max_attempts": 8,
        "retry_base_seconds": 30,
        "retry_cap_seconds": 3600,
        "min_claim_seconds": 120,
    }
    assert set(json.loads(capsys.readouterr().out)) == {"counts"}


def test_cli_work_reports_nonzero_when_task_limit_leaves_ready_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    queue = MemoryQueue(tmp_path)
    queue.enqueue("query", 1, {"number": 1})
    queue.enqueue("query", 1, {"number": 2})
    real_run_worker = queue_core.run_worker

    def worker(processor, **kwargs):
        del processor
        return real_run_worker(
            lambda task: True,
            processor_runner=queue_core.run_processor_inline,
            **kwargs,
        )

    monkeypatch.setattr(queue_core, "default_queue", lambda **kwargs: queue)
    monkeypatch.setattr(queue_core, "run_worker", worker)
    monkeypatch.setattr(
        sys,
        "argv",
        ["memory_queue.py", "work", "--max-tasks", "1", "--idle-seconds", "0"],
    )

    assert memory_queue.cli() == 1
    assert json.loads(capsys.readouterr().out)["counts"] == {
        "dead": 0,
        "failed": 0,
        "processed": 1,
        "remaining_eligible": 1,
        "skipped": 0,
        "succeeded": 1,
    }


def test_cli_work_forwards_policy_overrides(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, object] = {}

    def worker(processor, **kwargs):
        seen.update(kwargs)
        return queue_core.WorkerSummary(0, 0, 0, 0, 0)

    monkeypatch.setattr(queue_core, "run_worker", worker)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "memory_queue.py",
            "work",
            "--lease-seconds",
            "30",
            "--heartbeat-seconds",
            "10",
            "--max-attempts",
            "12",
            "--retry-base-seconds",
            "4",
            "--retry-cap-seconds",
            "40",
        ],
    )

    assert memory_queue.cli() == 0
    assert seen["lease_seconds"] == 30
    assert seen["heartbeat_seconds"] == 10
    assert seen["max_attempts"] == 12
    assert seen["retry_base_seconds"] == 4
    assert seen["retry_cap_seconds"] == 40
    assert set(json.loads(capsys.readouterr().out)) == {"counts"}


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (PermissionError("C:/secret/path"), "permission_denied"),
        (OSError("C:/secret/path"), "os_error"),
        (sqlite3.DatabaseError("C:/secret/db"), "sqlite_error"),
        (UnicodeDecodeError("utf-8", b"x", 0, 1, "secret"), "decode_error"),
        (subprocess.SubprocessError("secret command"), "process_error"),
    ],
)
def test_cli_expected_failures_emit_only_bounded_canonical_code(
    error: Exception,
    code: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["memory_queue.py", "status"])
    monkeypatch.setattr(
        memory_queue, "operator_status", lambda: (_ for _ in ()).throw(error)
    )

    assert memory_queue.cli() == 2
    captured = capsys.readouterr()
    assert captured.out == f'{{"codes":["{code}"]}}\n'
    assert captured.err == ""
    assert len(captured.out) < 128
    assert "secret" not in captured.out


def test_cli_unexpected_failure_emits_generic_internal_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["memory_queue.py", "status"])
    monkeypatch.setattr(
        memory_queue,
        "operator_status",
        lambda: (_ for _ in ()).throw(RuntimeError("C:/secret/payload")),
    )

    assert memory_queue.cli() == 2
    captured = capsys.readouterr()
    assert captured.out == '{"codes":["internal_error"]}\n'
    assert captured.err == ""


def _deny_memory_state_import(monkeypatch):
    import builtins

    original = builtins.__import__
    attempted = []

    def import_owner(name, *args, **kwargs):
        if name == 'memory_state':
            attempted.append(name)
            raise ImportError('state executor unavailable')
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', import_owner)
    return attempted


@pytest.mark.parametrize('state, expected', [('alive', True), ('dead', False), ('unknown', True)])
def test_queue_process_probe_does_not_load_state_executor(monkeypatch, state, expected):
    import process_liveness

    monkeypatch.setattr(process_liveness, 'process_state', lambda _pid: state)
    attempted = _deny_memory_state_import(monkeypatch)
    assert queue_core.pid_is_alive(4242) is expected
    assert attempted == []


@pytest.mark.parametrize('pid', [0, -1, True, False, '12', None, 1.5])
def test_queue_process_probe_retains_invalid_pid_contract(pid):
    assert queue_core.pid_is_alive(pid) is False


def test_queue_process_probe_retains_conservative_failure(monkeypatch):
    import process_liveness

    def refuse(_pid):
        raise OSError('process information unavailable')

    monkeypatch.setattr(process_liveness, 'pid_alive', refuse)
    assert queue_core.pid_is_alive(4242) is True


def test_queue_process_probe_has_no_state_import():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(queue_core.pid_is_alive))
    modules = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert 'memory_state' not in modules
    assert 'process_liveness' in modules
