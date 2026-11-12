"""A tool call runs under one absolute handler deadline: workers stay bounded, timed-out work never lands late, and late failures stay observable without secrets."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import pytest
from llm_wiki import mcp_server_limits

from tests.markdown_coordinator_parts import patch_coordinator
from tests.mcp_server_support import (
    _HUNG_PROCESS_SECONDS,
    TOOL_HELPERS,
    VALID_TOOL_CALLS,
    DrainAwareSet,
    ToolCallHarness,
    _assert_clean_shutdown,
    _assert_daily_untouched_after_timeout,
    _assert_late_failure_stderr,
    _assert_operation_timed_out,
    _assert_timed_out_envelope,
    _replace_in_every_server_part,
)
from tests.slow_machine import LONG_TIMEOUT, SHORT_TIMEOUT


class TestToolCallDeadlines(ToolCallHarness):
    """The handler deadline and the bounded worker slots of a tool call."""

    def test_grounded_deadline_starts_at_handler_entry_before_validation(self, monkeypatch):
        import mcp_server
        import query_memory

        real_validate = mcp_server.validate_tool_arguments
        deadlines = []
        now = [100.0]

        class Clock:
            @staticmethod
            def monotonic():
                return now[0]

        def delayed_validate(name, arguments):
            now[0] = 100.03
            return real_validate(name, arguments)

        def grounded_qa(*args, **kwargs):
            deadlines.append(kwargs["deadline"])
            return {
                "schema_version": "grounded-answer/v1",
                "status": "insufficient_evidence",
                "claims": [],
                "citations": [],
                "reason": "No evidence.",
            }

        _replace_in_every_server_part(monkeypatch, "time", Clock)
        monkeypatch.setattr("llm_wiki.mcp_server_validation.validate_tool_arguments", delayed_validate)
        monkeypatch.setattr(query_memory, "grounded_qa", grounded_qa)
        handler_start = now[0]

        self._run("recall", {"query": "question", "grounded": True})

        # A grounded answer has the grounded budget, counted from handler entry (A-17).
        assert deadlines[0] <= handler_start + query_memory.QA_DEADLINE_SECONDS

    def test_recall_second_timeout_uses_normal_error_envelope(self, monkeypatch):

        monkeypatch.setattr(
            "llm_wiki.mcp_server_vault.search_vault",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                TimeoutError("second retrieval timeout")
            ),
        )

        envelope = json.loads(self._run("recall", {"query": "needle"}))

        assert envelope["data"] == {"error": "operation_timeout"}
        assert envelope["partial"] is True
        assert envelope["coverage"] == 0

    def test_impact_reuses_handler_operation_deadline(self, monkeypatch):
        import mcp_server

        captured = []
        monkeypatch.setattr(
            "llm_wiki.mcp_server_architecture.analyze_change_impact",
            lambda **kwargs: captured.append(kwargs) or {"changes": []},
        )

        self._run(
            "get_architecture",
            {"directory": ".", "mode": "impact", "comparison": "dirty"},
        )

        assert captured[0]["deadline"] is not None
        assert captured[0]["deadline"] <= time.monotonic() + mcp_server.MCP_OPERATION_SECONDS

    @pytest.mark.parametrize("tool_name", VALID_TOOL_CALLS)
    def test_every_tool_receives_the_single_absolute_handler_deadline(
        self, monkeypatch, tool_name
    ):
        import mcp_server

        expected_data = {
            "recall": [],
            "read_page": {"ok": True},
            "wiki_overview": {"ok": True},
            "vault_status": {"ok": True},
            "get_decisions": [],
            "get_context": {},
            "check_contradiction": {
                "assessments": [],
                "evidence": [],
                "validity": {"status": "unverified"},
                "recommendations": ["quarantine"],
            },
            "log_decision": {"ok": True},
            "compile": {"ok": True},
            "find_dead_code": {"ok": True},
            "get_architecture": {"ok": True},
            "doctor": {"overall_status": "ok"},
        }
        seen = []

        class Clock:
            @staticmethod
            def monotonic():
                return 100.0

        def helper(*args, deadline, **kwargs):
            seen.append(deadline)
            return expected_data[tool_name]

        _replace_in_every_server_part(monkeypatch, "time", Clock)
        monkeypatch.setattr(TOOL_HELPERS[tool_name], helper)
        monkeypatch.setattr("llm_wiki.mcp_server_validation.meta", lambda **kwargs: {})

        envelope = json.loads(self._run(tool_name, VALID_TOOL_CALLS[tool_name]))

        assert "error" not in envelope["data"]
        # The tool's own budget: 10 s, or `mcp.retrieval_seconds` for recall and
        # get_decisions, whose answer waits for the reranker (B-9).
        budget = mcp_server.tool_operation_seconds(tool_name, VALID_TOOL_CALLS[tool_name])
        assert seen == [100.0 + budget]

    def test_get_context_does_not_replace_the_handler_deadline(self, monkeypatch):
        import corpus_snapshot
        import mcp_server

        seen = []
        now = [200.0]

        class Clock:
            @staticmethod
            def monotonic():
                return now[0]

        real_validate = mcp_server.validate_tool_arguments

        def delayed_validate(name, arguments):
            now[0] = 205.0
            return real_validate(name, arguments)

        def collect(*args, deadline, **kwargs):
            seen.append(deadline)
            raise TimeoutError("deadline reached")

        _replace_in_every_server_part(monkeypatch, "time", Clock)
        monkeypatch.setattr("llm_wiki.mcp_server_validation.validate_tool_arguments", delayed_validate)
        monkeypatch.setattr(corpus_snapshot, "collect_corpus", collect)

        envelope = json.loads(self._run("get_context", {"slugs": ["page"]}))

        assert seen == [200.0 + mcp_server.MCP_OPERATION_SECONDS]
        assert envelope["data"] == {"error": "operation_timeout"}

    def test_blocking_tool_does_not_block_event_loop_and_times_out(self, monkeypatch):
        import mcp_server

        release = threading.Event()
        finished = threading.Event()
        drained = threading.Event()
        workers = DrainAwareSet(drained)

        async def exercise():
            loop = asyncio.get_running_loop()
            started = asyncio.Event()

            def blocked(*, deadline):
                del deadline
                loop.call_soon_threadsafe(started.set)
                try:
                    release.wait(60.0)
                finally:
                    finished.set()
                return {"ok": True}

            monkeypatch.setattr("llm_wiki.mcp_server_vault.vault_status", blocked)
            task = asyncio.create_task(mcp_server.handle_tool_call("vault_status", {}))
            try:
                await asyncio.wait_for(started.wait(), timeout=0.5)
                await asyncio.sleep(0.01)
                loop_progressed = not finished.is_set()
                return loop_progressed, await task
            finally:
                release.set()
                assert finished.wait(SHORT_TIMEOUT)
                assert drained.wait(SHORT_TIMEOUT)
                with mcp_server_limits.MCP_WORKERS_LOCK:
                    assert not workers

        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_OPERATION_SECONDS", 3.0)
        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_WORKERS", workers)
        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_WORKERS_LOCK", threading.Lock())
        loop_progressed, text = asyncio.run(exercise())

        _assert_timed_out_envelope(json.loads(text), loop_progressed)

        expected = {"last_compile": "fresh", "last_compile_status": "ok"}
        monkeypatch.setattr(
            "llm_wiki.mcp_server_vault.vault_status",
            lambda *, deadline: expected,
        )
        sentinel = json.loads(
            asyncio.run(mcp_server.handle_tool_call("vault_status", {}))
        )
        assert sentinel["data"] == expected
        with mcp_server_limits.MCP_WORKERS_LOCK:
            assert not workers

    def test_timed_out_tool_workers_never_exceed_bounded_slots(self, monkeypatch):
        import threading

        import mcp_server

        release = threading.Event()
        all_done = threading.Event()
        lock = threading.Lock()
        active = 0
        peak = 0

        def leave():
            nonlocal active
            with lock:
                active -= 1
                if active == 0:
                    all_done.set()

        def blocked(*, deadline):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                release.wait(60.0)
            finally:
                leave()
            return {"ok": True}

        async def exercise():
            return await asyncio.gather(
                *(mcp_server.handle_tool_call("vault_status", {}) for _ in range(12))
            )

        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_OPERATION_SECONDS", 3.0)
        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_WORKERS", set())
        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_WORKERS_LOCK", threading.Lock())
        monkeypatch.setattr("llm_wiki.mcp_server_vault.vault_status", blocked)
        try:
            results = asyncio.run(exercise())
        finally:
            release.set()
            all_done.wait(0.5)

        errors = Counter(json.loads(item)["data"]["error"] for item in results)
        slots = mcp_server.MCP_WORKER_SLOTS
        assert (peak <= slots, errors) == (
            True,
            Counter(
                {"operation_timeout": slots, "worker_capacity_exhausted": 12 - slots}
            ),
        )

    def test_timed_out_blocked_worker_does_not_keep_process_alive(self):
        scripts = Path(__file__).resolve().parent.parent / "scripts"
        code = (
            "import asyncio,json,sys,threading; "
            f"sys.path.insert(0, {str(scripts)!r}); "
            "import mcp_server; from llm_wiki import mcp_server_limits, mcp_server_vault; "
            "mcp_server_limits.MCP_OPERATION_SECONDS=0.05; "
            "mcp_server_vault.vault_status=lambda *,deadline: threading.Event().wait(); "
            "result=asyncio.run(mcp_server.handle_tool_call('vault_status',{})); "
            "assert json.loads(result)['data']=={'error':'operation_timeout'}; "
            "print('TIMEOUT_RETURNED', flush=True)"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "TIMEOUT_RETURNED"
        shutdown_started = time.perf_counter()
        stdout, stderr = process.communicate(timeout=_HUNG_PROCESS_SECONDS)

        _assert_clean_shutdown(process, stdout, stderr, shutdown_started)

    def test_thread_start_failure_releases_reserved_worker_slot(self, monkeypatch):
        import threading

        import mcp_server

        class BrokenThread:
            def __init__(self, **kwargs):
                del kwargs

            def start(self):
                raise RuntimeError("cannot start thread")

        workers = set()
        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_WORKERS", workers)
        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_WORKERS_LOCK", threading.Lock())
        monkeypatch.setattr(threading, "Thread", BrokenThread)

        for _ in range(mcp_server.MCP_WORKER_SLOTS + 1):
            with pytest.raises(RuntimeError, match="cannot start"):
                asyncio.run(
                    mcp_server.run_bounded(
                        lambda: None, deadline=time.monotonic() + SHORT_TIMEOUT
                    )
                )

        assert workers == set()

    def test_timed_out_log_decision_never_appends_after_response(
        self, tmp_path, monkeypatch
    ):
        import threading
        from contextlib import contextmanager

        import mcp_server

        vault = tmp_path / "vault"
        state_root = tmp_path / "state"
        daily = vault / "knowledge/daily" / f"{datetime.now():%Y-%m-%d}.md"
        daily.parent.mkdir(parents=True)
        daily.write_bytes(b"# existing\n")
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

        monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
        monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))
        patch_coordinator(monkeypatch, 'begin_immediate', delayed_begin)
        # The operation has to reach its commit before the budget expires;
        # three seconds was not enough for that on a loaded Windows runner.
        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_OPERATION_SECONDS", 15.0)
        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_WORKERS", set())
        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_WORKERS_LOCK", threading.Lock())

        async def exercise():
            task = asyncio.create_task(
                mcp_server.handle_tool_call(
                    "log_decision", {"summary": "must not append late"}
                )
            )
            while not commit_reached.is_set() and not task.done():
                await asyncio.sleep(0.01)
            return commit_reached.is_set(), await task

        try:
            reached_commit, text = asyncio.run(exercise())
            assert reached_commit is True
            _assert_operation_timed_out(text)
            assert daily.read_bytes() == b"# existing\n"
        finally:
            release_commit.set()

        deadline = time.monotonic() + 30
        while mcp_server_limits.MCP_WORKERS and time.monotonic() < deadline:
            time.sleep(0.01)
        _assert_daily_untouched_after_timeout(daily)

    def test_late_worker_exception_is_observable_without_secret_text(
        self, monkeypatch, capsys
    ):
        import threading

        import mcp_server

        finished = threading.Event()
        sensitive = r"api_key=sk-abcdefghijklmnopqrstuvwxyz C:\private\vault\secret.md"
        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_WORKERS", set())
        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_WORKERS_LOCK", threading.Lock())

        def fail_late():
            time.sleep(0.3)
            try:
                raise RuntimeError(sensitive)
            finally:
                finished.set()

        with pytest.raises(TimeoutError):
            asyncio.run(
                mcp_server.run_bounded(
                    fail_late, deadline=time.monotonic() + 0.1
                )
            )
        assert finished.wait(SHORT_TIMEOUT)
        deadline = time.monotonic() + 1.0
        while mcp_server_limits.MCP_WORKERS and time.monotonic() < deadline:
            time.sleep(0.01)

        _assert_late_failure_stderr(capsys.readouterr().err, sensitive)
