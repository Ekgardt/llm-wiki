"""The MCP server helpers answer search, context, decisions, pages and code questions truthfully."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from tests.mcp_server_support import (
    _assert_architecture_report,
    _assert_context_package_contents,
    _assert_context_package_keys,
    _assert_dead_code_report,
    _assert_decision_filter_results,
    _assert_decision_filter_rows,
    _assert_decisions_search_call,
    _assert_degraded_result_row,
    _assert_degraded_search_budget,
    _assert_degraded_search_calls,
    _assert_degraded_trace,
    _assert_recall_search_call,
    _assert_vault_status_tool_description,
)


class TestHelperFunctions:
    """Test the helper functions that tools wrap."""

    def test_wiki_overview_returns_dict(self):
        from mcp_server import wiki_overview
        result = wiki_overview()
        assert isinstance(result, dict)
        assert "page_count" in result
        assert "retrieval_tier" in result

    def test_vault_status_returns_dict(self):
        from mcp_server import vault_status
        result = vault_status()
        assert isinstance(result, dict)
        assert "last_compile" in result

    def test_wiki_overview_uses_configured_vault_root(self, tmp_path, monkeypatch):
        import lookup_mode
        from mcp_server import wiki_overview

        monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
        monkeypatch.setattr(lookup_mode, "count_wiki_pages", lambda: 0)
        monkeypatch.setattr(lookup_mode, "index_status", lambda: {"available": False})

        result = wiki_overview()

        assert result["vault_root"] == str(tmp_path)

    def test_vault_status_wording_is_limited_to_compile_status_and_backlog(self):
        import mcp_server

        assert mcp_server.vault_status.__doc__ == (
            "Return compile status and current daily-file backlog."
        )
        tools = mcp_server.build_tool_definitions()
        if tools:
            _assert_vault_status_tool_description(tools)

    def test_vault_status_counts_changed_daily_files(self, tmp_path, monkeypatch):
        import memory_state
        from mcp_server import vault_status

        daily = tmp_path / "knowledge" / "daily"
        daily.mkdir(parents=True)
        unchanged = daily / "2026-07-12.md"
        changed = daily / "2026-07-13.md"
        unchanged.write_text("same", encoding="utf-8")
        changed.write_text("new", encoding="utf-8")
        monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
        monkeypatch.setattr(
            memory_state,
            "load_state",
            lambda: {
                "compiled_daily_hashes": {
                    unchanged.name: "hash-same",
                    changed.name: "old-hash",
                }
            },
        )
        monkeypatch.setattr(
            memory_state,
            "file_hash",
            lambda path: "hash-same" if path == unchanged else "hash-new",
        )

        assert vault_status()["compile_backlog"] == 1

    def test_vault_status_counts_unreadable_daily_conservatively(
        self, tmp_path, monkeypatch
    ):
        import memory_state
        from mcp_server import vault_status

        daily = tmp_path / "knowledge" / "daily"
        daily.mkdir(parents=True)
        unreadable = daily / "2026-07-13.md"
        unreadable.write_text("content", encoding="utf-8")
        monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
        monkeypatch.setattr(
            memory_state,
            "load_state",
            lambda: {"compiled_daily_hashes": {unreadable.name: "old"}},
        )

        def fail_hash(path):
            raise OSError("unreadable")

        monkeypatch.setattr(memory_state, "file_hash", fail_hash)

        assert vault_status()["compile_backlog"] == 1

    def test_search_vault_returns_list(self):
        from mcp_server import search_vault
        results = search_vault("test")
        assert isinstance(results, list)

    def test_search_vault_degrades_after_delayed_optional_initialization(self, monkeypatch):
        import mcp_server
        import search_memory

        calls = []

        def delayed_search(query, **kwargs):
            calls.append((query, kwargs))
            if len(calls) == 1:
                time.sleep(0.01)
                raise TimeoutError("retrieval deadline exceeded")
            return [
                {
                    "path": "knowledge/notes/test.md",
                    "requested_mode": "BASE",
                    "effective_mode": "BASE",
                    "signals_used": ["lexical"],
                    "fallback_reason": None,
                    "generation": "legacy",
                    "partial": False,
                }
            ]

        monkeypatch.setattr(search_memory, "search", delayed_search)

        results = mcp_server.search_vault("test")

        _assert_degraded_search_calls(calls)
        _assert_degraded_search_budget(calls)
        _assert_degraded_result_row(results[0])
        _assert_degraded_trace(
            results[0], mcp_server.retrieval_trace("test", results)
        )

    def test_search_vault_reuses_caller_operation_deadline(self, monkeypatch):
        import mcp_server
        import search_memory

        calls = []

        def search(_query, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise TimeoutError("semantic timeout")
            return []

        monkeypatch.setattr(search_memory, "search", search)
        deadline = time.monotonic() + 5

        mcp_server.search_vault("query", deadline=deadline)

        # The hybrid pass keeps back the lexical reserve; the fallback runs to the end (B-18).
        reserve = mcp_server.LEXICAL_FALLBACK_RESERVE_SECONDS
        assert [call["deadline_monotonic"] for call in calls] == [deadline - reserve, deadline]

    def test_search_vault_propagates_second_timeout(self, monkeypatch):
        import mcp_server
        import search_memory

        calls = 0

        def search(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise TimeoutError(f"attempt {calls} timed out")

        monkeypatch.setattr(search_memory, "search", search)

        with pytest.raises(TimeoutError, match="attempt 2"):
            mcp_server.search_vault("query", deadline=time.monotonic() + 5)

    def test_search_vault_preserves_hybrid_signals_completed_under_deadline(
        self, monkeypatch
    ):
        import mcp_server
        import search_memory

        expected = [{
            "candidate_id": "dense",
            "path": "dense.md",
            "requested_mode": "HYBRID",
            "effective_mode": "HYBRID",
            "signals_used": ["lexical", "dense"],
            "fallback_reason": None,
            "generation": "legacy",
            "partial": False,
        }]
        calls = []

        def search(*_args, **kwargs):
            calls.append(kwargs)
            return expected

        monkeypatch.setattr(search_memory, "search", search)

        rows = mcp_server.search_vault(
            "query", deadline=time.monotonic() + 5
        )

        assert rows == expected
        assert len(calls) == 1
        assert calls[0]["semantic"] is True

    def test_grounded_candidate_retrieval_preserves_hybrid_trace_under_deadline(
        self, monkeypatch
    ):
        import query_memory
        import retrieval

        expected = [{"candidate_id": "dense", "signals_used": ["lexical", "dense"]}]
        captured = []

        def retrieve(*_args, **kwargs):
            captured.append(kwargs)
            return expected

        monkeypatch.setattr(retrieval, "retrieve_via_search_memory", retrieve)
        deadline = time.monotonic() + 5

        rows = query_memory.default_candidates(
            "question", profile="HYBRID", deadline=deadline
        )

        assert rows == tuple(expected)
        assert captured[0]["deadline_monotonic"] == deadline
        assert captured[0]["semantic"] is True

    def test_get_context_batch(self, tmp_path, monkeypatch):
        import mcp_server

        (tmp_path / "knowledge/notes").mkdir(parents=True)
        monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))

        result = mcp_server.get_context(["nonexistent-1", "nonexistent-2"])

        assert result["repo_map"] == []
        assert result["missing_slugs"] == ["nonexistent-1", "nonexistent-2"]

    def test_get_context_returns_one_bounded_compiler_package(self, tmp_path, monkeypatch):
        import mcp_server

        notes = tmp_path / "knowledge/notes"
        projects = tmp_path / "knowledge/projects/demo"
        notes.mkdir(parents=True)
        projects.mkdir(parents=True)
        (notes / "choice.md").write_text(
            "---\ntype: decision\nstatus: active\n---\n# Choice\n\n"
            "One-sentence summary: Keep one package.\n\n## Evidence\n\nproof\n",
            encoding="utf-8",
        )
        (notes / "incident.md").write_text(
            "---\ntype: debugging\nstatus: active\n---\n# Incident\n\n"
            "One-sentence summary: Compiler regression.\n",
            encoding="utf-8",
        )
        (projects / "state.md").write_text(
            "---\ntype: project-state\nstatus: active\n---\n# Demo\n\n"
            "One-sentence summary: Active Task17.\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))

        package = mcp_server.get_context(
            ["choice", "incident", "state"], token_budget=1200
        )

        _assert_context_package_keys(package)
        _assert_context_package_contents(package)

    def test_get_context_repo_map_includes_selected_pages_dropped_by_budget(
        self, tmp_path, monkeypatch
    ):
        import mcp_server

        notes = tmp_path / "knowledge/notes"
        notes.mkdir(parents=True)
        for slug in ("alpha", "beta"):
            (notes / f"{slug}.md").write_text(
                "---\ntype: concept\nstatus: active\n---\n"
                f"# {slug.title()}\n\nOne-sentence summary: " + (slug * 300) + "\n",
                encoding="utf-8",
            )
        monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))

        # 512 holds the bytes 256 did under the earlier 4-bytes estimate; at the
        # measured 2 bytes per token, 256 cannot hold this answer's item list.
        package = mcp_server.get_context(["alpha", "beta"], token_budget=512)

        assert package["repo_map"] == [
            "knowledge/notes/alpha.md",
            "knowledge/notes/beta.md",
        ]

    def test_get_context_direct_call_deduplicates_and_enforces_bounds(
        self, tmp_path, monkeypatch
    ):
        import mcp_server

        (tmp_path / "knowledge/notes").mkdir(parents=True)
        monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
        reads = []
        monkeypatch.setattr(
            "llm_wiki.mcp_server_vault.read_page",
            lambda slug, **kwargs: reads.append(slug) or {"slug": slug, "content": ""},
        )

        package = mcp_server.get_context(["first", "first", "second"])
        assert package["missing_slugs"] == ["first", "second"]
        assert reads == []
        requested = [f"page-{index}" for index in range(21)]
        assert mcp_server.get_context(requested)["missing_slugs"] == sorted(requested)
        with pytest.raises(ValueError, match="slug"):
            mcp_server.get_context(["x" * 256])
        included = [f"compatibility-{index}-" + "x" * 65 for index in range(11)]
        assert mcp_server.get_context(["page"], included)["include"] == included
        with pytest.raises(ValueError, match="include"):
            mcp_server.get_context(["page"], [True])

    def test_recall_and_decisions_pass_distinct_source_tools(self, monkeypatch):
        import mcp_server
        import search_memory

        calls = []
        monkeypatch.setattr(
            search_memory,
            "search",
            lambda query, **kwargs: calls.append((query, kwargs)) or [],
        )

        assert mcp_server.search_vault("secret", 2) == []
        assert mcp_server.get_decisions("choice", 3) == []
        _assert_recall_search_call(calls[0])
        _assert_decisions_search_call(calls[1])

    def test_get_decisions_records_only_filtered_final_candidates(self, tmp_path, monkeypatch):
        import mcp_server
        import retrieval_telemetry
        import search_memory

        database = tmp_path / "cache/evidence-graph/telemetry.sqlite3"
        monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))
        monkeypatch.setattr(
            search_memory,
            "search",
            lambda query, **kwargs: [
                {"path": "knowledge/notes/concept.md", "type": "concept"},
                {"path": "knowledge/notes/first-decision.md", "type": "decision"},
                {"path": "knowledge/notes/second-decision.md", "type": "decision"},
            ],
        )

        results = mcp_server.get_decisions("private decision query", 10)

        _assert_decision_filter_results(results)
        _assert_decision_filter_rows(
            list(reversed(retrieval_telemetry.read_events(limit=10, db_path=database)))
        )
        assert b"private decision query" not in database.read_bytes()

    def test_get_decisions_empty_result_emits_no_events(self, tmp_path, monkeypatch):
        import mcp_server
        import retrieval_telemetry
        import search_memory

        database = tmp_path / "cache/evidence-graph/telemetry.sqlite3"
        monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))
        monkeypatch.setattr(search_memory, "search", lambda query, **kwargs: [])

        assert mcp_server.get_decisions("none", 10) == []
        assert retrieval_telemetry.read_events(limit=10, db_path=database) == []

    def test_context_emits_injection_without_duplicate_page_read(self, tmp_path, monkeypatch):
        import mcp_server
        import retrieval_telemetry

        notes = tmp_path / "vault/knowledge/notes"
        notes.mkdir(parents=True)
        (notes / "page.md").write_text("# Context Page\n", encoding="utf-8")
        database = tmp_path / "state/cache/evidence-graph/telemetry.sqlite3"
        monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path / "vault"))
        monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "state"))

        package = mcp_server.get_context(["page"])
        assert "knowledge/notes/page.md" in package["repo_map"]

        rows = retrieval_telemetry.read_events(limit=10, db_path=database)
        # The injected page is named by its vault-relative path, the one
        # identity this column carries.
        assert [(row.event_kind, row.candidate_id, row.source_tool) for row in rows] == [
            ("context_injected", "knowledge/notes/page.md", "mcp.get_context")
        ]

    def test_trigger_compile_returns_dict(self, monkeypatch):
        import compile_memory
        from mcp_server import trigger_compile

        monkeypatch.setattr(compile_memory, "run_pending_compile", lambda **kwargs: 0)
        result = trigger_compile()
        assert isinstance(result, dict)

    def test_trigger_compile_runs_under_handler_deadline_and_cancellation(
        self, monkeypatch
    ):
        import compile_memory
        import maybe_compile
        import mcp_server

        received = []

        def cancelled():
            return False

        monkeypatch.setattr("llm_wiki.mcp_server_workers.operation_cancelled", lambda: cancelled)
        monkeypatch.setattr(
            compile_memory,
            "run_pending_compile",
            lambda **kwargs: received.append(kwargs) or 0,
            raising=False,
        )
        monkeypatch.setattr(
            maybe_compile,
            "spawn_compile_if_idle",
            lambda **_kwargs: pytest.fail("MCP compile must not detach unbounded work"),
        )
        deadline = time.monotonic() + 5

        result = mcp_server.trigger_compile(deadline=deadline)

        assert result == {"status": "completed", "returncode": 0}
        assert received == [
            {"trigger": "manual", "deadline": deadline, "cancelled": cancelled}
        ]

    def test_code_tools_reject_empty_missing_file_and_drive_root(self, tmp_path):
        import mcp_server

        missing = tmp_path / "missing"
        regular_file = tmp_path / "file.py"
        regular_file.write_text("", encoding="utf-8")
        drive_root = Path(tmp_path.anchor)

        for value in ("", str(missing), str(regular_file), str(drive_root)):
            for tool in (mcp_server.find_dead_code_answer, mcp_server.architecture_answer):
                result = tool(value)
                assert "error" in result

    def test_code_tools_do_not_fall_back_to_cwd(self, monkeypatch):
        import mcp_server

        called = []
        monkeypatch.setattr("llm_wiki.code_graph_dead_code.find_dead_code", lambda directory: called.append(directory))

        result = mcp_server.find_dead_code_answer("")

        assert "error" in result
        assert called == []

    def test_code_tool_helpers_forward_store_report(self, tmp_path, monkeypatch):
        import mcp_server

        report = {
            "source_generation": "gen-25",
            "graph_complete": False,
            "unresolved_count": 2,
            "fallback": False,
        }
        monkeypatch.setattr(
            "llm_wiki.code_graph_dead_code.find_dead_code",
            lambda directory, **options: {"candidates": [{"name": "unused"}], **report},
        )
        monkeypatch.setattr(
            "llm_wiki.code_graph_architecture.get_architecture",
            lambda directory, **options: {
                "entry_points": [],
                "routes": [],
                "hotspots": [],
                "communities": [],
                **report,
            },
        )

        dead = mcp_server.find_dead_code_answer(str(tmp_path))
        architecture = mcp_server.architecture_answer(str(tmp_path))

        _assert_dead_code_report(dead)
        _assert_architecture_report(architecture)

    def test_code_tool_helpers_forward_explicit_live_fallback(self, tmp_path, monkeypatch):
        import mcp_server

        seen = []
        monkeypatch.setattr(
            "llm_wiki.code_graph_dead_code.find_dead_code",
            lambda directory, **options: seen.append(options) or [],
        )

        mcp_server.find_dead_code_answer(str(tmp_path), live=True)

        # `symbol` travels down since 2026-09-12: a question about one name reads
        # only the sources that mention it instead of the whole repository.
        assert seen == [{"live": True, "with_report": True, "symbol": None}]
