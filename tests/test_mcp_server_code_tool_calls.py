"""The code tool calls dispatch graph modes, never answer another repository from the active graph, and envelope generation reports and dead-code verdicts."""
from __future__ import annotations

import asyncio
import json
import sys

import pytest

from tests.mcp_server_support import (
    ToolCallHarness,
    _assert_code_tool_data,
    _assert_dead_code_verdict,
    _assert_graph_mode_dispatch,
    _assert_low_confidence_envelope,
    _assert_no_foreign_generation,
    _assert_no_foreign_paths,
    _assert_summary_keeps_counts_and_names_the_mode,
)


class TestCodeToolCalls(ToolCallHarness):
    """Architecture and dead-code answers through the MCP tool call."""

    @pytest.mark.parametrize(
        ("mode", "args", "helper", "result_key"),
        [
            ("callers", {"symbol": "target"}, "find_callers", "callers"),
            ("callees", {"symbol": "target"}, "find_callees", "callees"),
            ("dependencies", {"symbol": "target"}, "find_dependencies", "dependencies"),
            ("path", {"symbol": "source", "target": "sink"}, "find_paths", "paths"),
            ("community", {}, "detect_communities", "communities"),
        ],
    )
    def test_get_architecture_dispatches_graph_modes(
        self, tmp_path, monkeypatch, mode, args, helper, result_key
    ):
        import code_graph

        calls = []
        monkeypatch.setattr(
            sys.modules[getattr(code_graph, helper).__module__],
            helper,
            lambda *values, **options: calls.append((values, options))
            or {result_key: [], "source_generation": None, "graph_complete": False,
                "unresolved_count": 0, "fallback": True},
        )

        data = self._data(
            "get_architecture", {"directory": str(tmp_path), "mode": mode, **args}
        )

        _assert_graph_mode_dispatch(data, calls, mode, result_key)

    def test_get_architecture_symbol_mode_combines_existing_graph_queries(
        self, tmp_path, monkeypatch
    ):

        report = {
            "source_generation": "graph-17",
            "graph_complete": True,
            "unresolved_count": 0,
            "fallback": False,
        }
        monkeypatch.setattr(
            "llm_wiki.code_graph_calls.find_callers", lambda *args, **kwargs: {"callers": [1], **report}
        )
        monkeypatch.setattr(
            "llm_wiki.code_graph_calls.find_callees", lambda *args, **kwargs: {"callees": [2], **report}
        )
        monkeypatch.setattr(
            "llm_wiki.code_graph_dependencies.find_dependencies",
            lambda *args, **kwargs: {"dependencies": [3], **report},
        )

        envelope = json.loads(
            self._run(
                "get_architecture",
                {"directory": str(tmp_path), "mode": "symbol", "symbol": "target"},
            )
        )

        assert envelope["data"]["architecture"] == {
            "symbol": "target",
            # Where the symbol is defined, added 2026-09-12: the question the
            # mode is asked most often is "where is this", and the answer used to
            # carry every line around the definition except its own.
            "definition": [],
            "callers": [1],
            "callees": [2],
            "dependencies": [3],
            **report,
        }
        # No generation of tmp_path to compare commits with: not known to be fresh
        # (audit B-34, docs/research/2026-09-25-a-structural-answer-says-its-own-freshness.md).
        assert envelope["components"]["graph"] == {
            "generation": "graph-17",
            "freshness": "unknown",
        }

    def test_external_repository_never_receives_active_repository_graph(
        self, tmp_path, monkeypatch
    ):
        from llm_wiki.generation_catalog_store import GenerationCatalog
        from repository_scope import resolve_repository_scope

        from tests.evidence_graph_recovery_helpers import _publish, _rich_graph_records

        repository_a = tmp_path / "owner-a" / "project"
        repository_b = tmp_path / "owner-b" / "project"
        repository_a.mkdir(parents=True)
        repository_b.mkdir(parents=True)
        (repository_b / "b.py").write_text(
            "def b_only_symbol():\n    pass\n", encoding="utf-8"
        )
        records = _rich_graph_records()
        for node in records["nodes"]:
            node["metadata"]["name"] = "a_only_symbol"
        state = tmp_path / "shared-state"
        catalog = GenerationCatalog(state)
        _publish(
            catalog,
            "repo-a",
            graph_records=records,
            repository_scope=resolve_repository_scope(repository_a).as_dict(),
        )
        catalog.register("repo-a")
        assert catalog.activate("repo-a", expected_active=None)
        monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))

        dead = json.loads(
            self._run("find_dead_code", {"directory": str(repository_b)})
        )
        architecture = json.loads(
            self._run("get_architecture", {"directory": str(repository_b)})
        )

        for envelope in (dead, architecture):
            _assert_no_foreign_generation(envelope)
            _assert_no_foreign_paths(envelope, repository_a, repository_b)

    def test_find_dead_code_returns_candidates(self, tmp_path, monkeypatch):
        expected = [{"name": "unused", "status": "candidate", "graph_complete": False}]
        monkeypatch.setattr("llm_wiki.code_graph_dead_code.find_dead_code", lambda directory, **options: expected)

        envelope = json.loads(self._run("find_dead_code", {"directory": str(tmp_path)}))
        _assert_code_tool_data(envelope["data"], "candidates", expected, tmp_path)
        _assert_low_confidence_envelope(envelope)

    def test_get_architecture_returns_summary(self, tmp_path, monkeypatch):
        """The summary passes the architecture through, minus the member dump.

        `communities` was 13 313 of this answer's 20 076 architecture tokens on
        this repository (measured 2026-08-29) and is verbatim what
        `mode=community` already returns, so the summary names that mode
        instead of repeating it. Every other field, and every community count,
        is unchanged.
        """
        architecture = {
            "entry_points": [{"name": "main"}],
            "routes": [],
            "hotspots": [],
            "communities": [{"size": 2, "members": [{"name": "a"}, {"name": "b"}]}],
            "community_count": 1,
            "community_member_count": 2,
            "graph_complete": False,
        }
        monkeypatch.setattr(
            "llm_wiki.code_graph_architecture.get_architecture", lambda directory, **options: architecture
        )

        envelope = json.loads(self._run("get_architecture", {"directory": str(tmp_path)}))
        _assert_summary_keeps_counts_and_names_the_mode(envelope, architecture, tmp_path)
        _assert_low_confidence_envelope(envelope)

    def test_find_dead_code_answers_about_one_named_symbol(self, tmp_path, monkeypatch):
        """A verdict about the name asked for, not the whole repository listing.

        Measured 2026-08-29: the unanchored listing nominates 846 candidates,
        delivers 532 at the client ceiling and cuts 307 named rows, so absence
        from it is indistinguishable from having living callers. The anchored
        form answers the question the caller asked, in 250 tokens against
        24 982.
        """
        listing = {
            "candidates": [
                {"name": "_alive", "file": "a.py", "line": 1},
                {"name": "_dead", "file": "b.py", "line": 2},
            ],
            "candidate_count": 2,
            "graph_complete": False,
        }
        monkeypatch.setattr(
            "llm_wiki.code_graph_dead_code.find_dead_code", lambda directory, **options: listing
        )

        _assert_dead_code_verdict(
            self._dead_code_for(tmp_path, "_dead"), "candidate", ["_dead"]
        )
        _assert_dead_code_verdict(
            self._dead_code_for(tmp_path, "_absent"), "not_a_candidate", []
        )

    def _dead_code_for(self, tmp_path, symbol: str) -> dict:
        arguments = {"directory": str(tmp_path), "symbol": symbol}
        return json.loads(self._run("find_dead_code", arguments))["data"]

    def test_code_tool_envelope_uses_generation_report(self, tmp_path, monkeypatch):
        import mcp_server

        monkeypatch.setattr(
            "llm_wiki.mcp_server_architecture.find_dead_code_answer",
            lambda directory, **options: {
                "directory": directory,
                "candidates": [],
                "source_generation": "gen-25",
                "graph_complete": True,
                "unresolved_count": 0,
                "fallback": False,
            },
        )

        envelope = json.loads(
            asyncio.run(
                mcp_server.handle_tool_call(
                    "find_dead_code", {"directory": str(tmp_path)}
                )
            )
        )

        assert envelope["fallback"] is False
        assert envelope["partial"] is False
        assert envelope["coverage"] >= 0.9
        assert envelope["data"]["source_generation"] == "gen-25"
