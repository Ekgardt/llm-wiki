"""The doctor tool call receives the exact handler deadline, rolls back timed-out queue mutations, and answers rejected or failed operator actions as protocol errors."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time

import pytest

from tests.markdown_coordinator_parts import patch_coordinator
from tests.mcp_server_support import (
    ToolCallHarness,
    _assert_doctor_status_envelope,
    _assert_queue_unchanged_after_timeout,
    _assert_rejected_queue_action,
    _assert_timed_out_queue_call,
    _drain_mcp_workers,
    _expected_task_state,
    _replace_in_every_server_part,
)
from tests.slow_machine import LONG_TIMEOUT


class TestDoctorToolCalls(ToolCallHarness):
    """Doctor queue and transaction actions through the MCP tool call."""

    @pytest.mark.parametrize("action", ["queue-cancel", "queue-redrive"])
    def test_queue_mutations_receive_exact_doctor_deadline(self, monkeypatch, action):
        import mcp_server
        import queue_core

        received = []

        class Queue:
            def __init__(self, root, *, deadline=None):
                del root, deadline

            def cancel(self, target_id, *, deadline, cancelled):
                received.append((target_id, deadline, cancelled()))
                return True

            def redrive(self, target_id, *, deadline, cancelled):
                received.append((target_id, deadline, cancelled()))
                return "replacement"

        monkeypatch.setattr(queue_core, "MemoryQueue", Queue)
        deadline = time.monotonic() + 5

        result = mcp_server.doctor(
            action=action, target_id="task", repair=True, deadline=deadline
        )

        assert result["overall_status"] == "ok"
        assert received == [("task", deadline, False)]

    def test_transaction_undo_and_apply_receive_exact_doctor_deadline(self, monkeypatch):
        import mcp_server

        received = []

        class Record:
            id = "undo"
            state = "prepared"
            error_code = None

        class AppliedRecord:
            id = "undo"
            state = "committed"
            error_code = None

        class Coordinator:
            def __init__(self, *args, deadline=None):
                del args, deadline

            def undo(self, target_id, *, deadline, cancelled):
                received.append(("undo", target_id, deadline, cancelled()))
                return Record()

            def apply(self, transaction_id, *, deadline, cancelled):
                received.append(("apply", transaction_id, deadline, cancelled()))
                return AppliedRecord()

        patch_coordinator(monkeypatch, "MarkdownCoordinator", Coordinator)
        deadline = time.monotonic() + 5

        result = mcp_server.doctor(
            action="transaction-undo",
            target_id="original",
            repair=True,
            deadline=deadline,
        )

        assert result["overall_status"] == "ok"
        assert received == [
            ("undo", "original", deadline, False),
            ("apply", "undo", deadline, False),
        ]

    @pytest.mark.parametrize("action", ["queue-cancel", "queue-redrive"])
    def test_timed_out_queue_mutation_rolls_back_delayed_sql_commit(
        self, tmp_path, monkeypatch, action
    ):
        from contextlib import contextmanager

        import mcp_server
        import queue_core
        from llm_wiki import memory_queue_failures

        state_root = tmp_path / "state"
        vault = tmp_path / "vault"
        vault.mkdir()
        queue = queue_core.MemoryQueue(state_root)
        task_id = queue.enqueue("query", 1, {})
        if action == "queue-redrive":
            lease = queue.claim("worker")
            assert lease is not None
            queue.fail(lease, queue_core.QueueFailure("invalid_input", permanent=True))

        commit_reached = threading.Event()
        release_commit = threading.Event()

        @contextmanager
        def delayed_begin(connection, *, before_commit=None):
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                commit_reached.set()
                assert release_commit.wait(LONG_TIMEOUT)
                if before_commit is not None:
                    before_commit()
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

        monkeypatch.setattr(memory_queue_failures, "begin_immediate", delayed_begin)
        monkeypatch.setattr(queue_core, "MemoryQueue", lambda _root, *, deadline=None: queue)
        monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
        monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))
        # The operation has to reach its commit before the budget expires;
        # three seconds was not enough for that on a loaded Windows runner.
        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_OPERATION_SECONDS", 15.0)
        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_WORKERS", set())
        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_WORKERS_LOCK", threading.Lock())

        try:
            text = asyncio.run(
                mcp_server.handle_tool_call(
                    "doctor",
                    {"action": action, "target_id": task_id, "repair": True},
                )
            )
            _assert_timed_out_queue_call(text, commit_reached)
            assert queue.get(task_id).state == _expected_task_state(action)
        finally:
            release_commit.set()

        _drain_mcp_workers()
        _assert_queue_unchanged_after_timeout(queue, task_id, action)

    def test_doctor_receives_exact_handler_deadline_after_dispatch_delay(
        self, monkeypatch
    ):
        import doctor
        import mcp_server

        now = [100.0]
        captured = []
        real_validate = mcp_server.validate_tool_arguments

        class Clock:
            @staticmethod
            def monotonic():
                return now[0]

        def delayed_validate(name, arguments):
            now[0] = 104.0
            return real_validate(name, arguments)

        def run_doctor(**kwargs):
            captured.append(kwargs)
            return {
                "overall_status": "ok",
                "scope": "operational",
                "coverage": {"retained_history": "not_checked"},
                "generated_at": "2026-10-17T00:00:00+00:00",
                "checks": [],
                "counts": {"ok": 0, "degraded": 0, "error": 0},
                "run_deletion": {"blockers": []},
            }

        _replace_in_every_server_part(monkeypatch, "time", Clock)
        monkeypatch.setattr("llm_wiki.mcp_server_validation.validate_tool_arguments", delayed_validate)
        monkeypatch.setattr(doctor, "run_doctor", run_doctor)

        envelope = json.loads(self._run("doctor", {"action": "status"}))

        assert envelope["data"]["overall_status"] == "ok"
        assert captured[0]["deadline"] == 100.0 + mcp_server.MCP_OPERATION_SECONDS
        assert "time_budget_seconds" not in captured[0]
        assert captured[0]["scope"] == "operational"
        assert envelope["data"]["coverage"]["retained_history"] == "not_checked"

    def test_doctor_returns_uniform_conservative_envelope(self, monkeypatch):

        monkeypatch.setattr(
            "llm_wiki.mcp_server_doctor.doctor",
            lambda **kwargs: {
                "overall_status": "degraded",
                "checks": [{"id": "index", "status": "degraded", "message": "stale"}],
            },
        )

        envelope = json.loads(self._run("doctor", {"action": "status"}))

        _assert_doctor_status_envelope(envelope)
        assert envelope["warnings"]

    def test_doctor_operator_failure_is_protocol_error_with_error_quality(
        self, monkeypatch
    ):

        monkeypatch.setattr(
            "llm_wiki.mcp_server_doctor.doctor",
            lambda **kwargs: {
                "action": kwargs["action"],
                "overall_status": "error",
                "ids": [],
                "counts": {"items": 0},
                "states": ["error"],
                "codes": ["operation_failed"],
            },
        )

        envelope = json.loads(
            self._run("doctor", {"action": "transaction-recover", "repair": True})
        )

        assert envelope["data"]["overall_status"] == "error"
        assert envelope["partial"] is True
        assert envelope["confidence"] < 0.5

    def test_transaction_recover_passes_limit_and_deadline_not_post_slice(
        self, monkeypatch
    ):
        received = []

        class Coordinator:
            def __init__(self, *args, deadline=None):
                del args, deadline

            def recover(self, **kwargs):
                received.append(kwargs)
                return []

        patch_coordinator(monkeypatch, "MarkdownCoordinator", Coordinator)

        envelope = json.loads(
            self._run(
                "doctor",
                {"action": "transaction-recover", "repair": True, "limit": 7},
            )
        )

        assert envelope["data"]["overall_status"] == "ok"
        assert received[0]["max_transactions"] == 7
        assert received[0]["deadline"] > time.monotonic()

    @pytest.mark.parametrize(
        ("action", "behavior", "expected_code"),
        [
            ("queue-inspect", "missing", "unknown_task"),
            ("queue-cancel", "false", "unknown_or_terminal_task"),
            ("queue-redrive", "missing", "unknown_task"),
            ("queue-redrive", "invalid", "redrive_requires_dead"),
        ],
    )
    def test_rejected_queue_actions_use_error_quality_and_protocol_error(
        self, tmp_path, monkeypatch, action, behavior, expected_code
    ):
        import mcp_server
        import queue_core

        state_root = tmp_path / "state"
        run = state_root / "run"
        run.mkdir(parents=True)
        monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))
        if action == "queue-inspect":
            database = run / "queue.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE tasks(id, state, error_code)")
        else:
            class Queue:
                def __init__(self, root, *, deadline=None):
                    del root, deadline

                def cancel(self, target_id, **kwargs):
                    del kwargs
                    return False

                def redrive(self, target_id, **kwargs):
                    del kwargs
                    if behavior == "missing":
                        raise KeyError(target_id)
                    raise queue_core.QueueOperationError("redrive_requires_dead")

            monkeypatch.setattr(queue_core, "MemoryQueue", Queue)

        data = mcp_server.doctor(
            action=action,
            target_id="missing-task",
            repair=action != "queue-inspect",
        )
        monkeypatch.setattr("llm_wiki.mcp_server_doctor.doctor", lambda **kwargs: data)
        text = asyncio.run(
            mcp_server.handle_tool_call(
                "doctor",
                {
                    "action": action,
                    "target_id": "missing-task",
                    **({"repair": True} if action != "queue-inspect" else {}),
                },
            )
        )
        envelope = json.loads(text)

        _assert_rejected_queue_action(data, envelope, expected_code)

        class Model:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        monkeypatch.setattr("llm_wiki.mcp_server_limits.TextContent", Model)
        monkeypatch.setattr("llm_wiki.mcp_server_limits.CallToolResult", Model)
        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_CALL_TOOL_RESULT_AVAILABLE", True)
        assert mcp_server.format_tool_result(text).isError is True

    def test_queue_inspect_missing_database_is_protocol_error(self, tmp_path, monkeypatch):
        import mcp_server

        state_root = tmp_path / "state"
        state_root.mkdir()
        monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))

        data = mcp_server.doctor(action="queue-inspect", target_id="task")

        assert data["overall_status"] == "error"
        assert data["codes"] == ["queue_missing"]
