"""Warm-up appears in the health answer, and a long query reaches retrieval whole."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time

import pytest

from tests.mcp_server_support import (
    TOOL_HELPERS,
    _warned_about,
)
from tests.slow_machine import LONG_TIMEOUT


class TestWarmupIsInTheHealthAnswer:
    """Audit OPS-13: a failed warm-up is recorded and shown, never swallowed."""

    def test_a_failed_stage_is_named_in_the_health_resource(self, monkeypatch, capsys):
        import mcp_server

        def broken_reranker():
            raise RuntimeError("no torch")

        monkeypatch.setattr("llm_wiki.mcp_server_warmup.warm_reranker", broken_reranker)
        mcp_server.warmup_retrieval_path()

        state = mcp_server.warmup_state()
        assert (state["status"], state["stage"], state["error"]) == (
            "failed",
            "reranker",
            "RuntimeError",
        )
        printed = capsys.readouterr().err
        monkeypatch.setattr(
            "llm_wiki.mcp_server_vault.vault_status",
            lambda **_k: {
                "last_compile": "2026-09-10T03:00:00",
                "last_compile_status": "ok",
                "compile_backlog": 0,
                "warmup": state,
            },
        )
        envelope = json.loads(mcp_server.handle_resource_read("llm-wiki://health"))

        assert (
            "retrieval warm-up failed at reranker: RuntimeError: no torch" in printed,
            envelope["partial"],
            _warned_about(envelope, "warm-up failed at reranker"),
            envelope["data"]["warmup"]["status"],
        ) == (True, True, True, "failed")

    def test_a_completed_warm_up_records_its_seconds(self, monkeypatch):
        import mcp_server

        monkeypatch.setattr("llm_wiki.mcp_server_warmup.warm_reranker", lambda: None)
        monkeypatch.setattr("llm_wiki.mcp_server_warmup.warmup_pass", lambda _seconds: None)

        mcp_server.warmup_retrieval_path()

        state = mcp_server.warmup_state()
        assert state["status"] == "warm"
        assert isinstance(state["seconds"], float)


@pytest.mark.parametrize("tool", ["recall", "get_decisions"])
def test_a_long_query_reaches_retrieval_whole_under_the_callers_deadline(monkeypatch, tool):
    import mcp_server
    import search_memory

    query = "evidence " * 1025
    received = []
    monkeypatch.setattr(search_memory, "search", lambda text, **kwargs: received.append((text, kwargs)) or [])
    monkeypatch.setattr("llm_wiki.mcp_server_vault.record_decision_impressions", lambda *_args: None)
    calls = {"recall": mcp_server.search_vault, "get_decisions": mcp_server.get_decisions}
    deadline = time.monotonic() + LONG_TIMEOUT
    assert calls[tool](query, deadline=deadline) == []
    assert received[0][0] == query
    assert received[0][1]["deadline_monotonic"] <= deadline


@pytest.mark.parametrize("invalid", [True, 42, [], {}])
def test_removing_query_length_refusal_preserves_type_validation(invalid):
    import mcp_server

    with pytest.raises(ValueError, match="query must be a string"):
        mcp_server.search_vault(invalid)
    with pytest.raises(ValueError, match="query must be a string"):
        mcp_server.get_decisions(invalid)


@pytest.mark.parametrize("tool", ["recall", "get_decisions"])
def test_public_query_schema_dispatches_a_long_string_whole(monkeypatch, tool):
    import mcp_server

    query = "evidence " * 1025
    received = []
    monkeypatch.setattr(TOOL_HELPERS[tool], lambda text, *args, **kwargs: received.append(text) or [])
    envelope = json.loads(asyncio.run(mcp_server.handle_tool_call(tool, {"query": query})))
    assert envelope["data"]["results"] == []
    assert "error" not in envelope["data"]
    assert received == [query]


def test_a_page_accepted_by_knowledge_remains_readable_and_hashable_by_mcp(tmp_path, monkeypatch):
    import mcp_server

    notes = tmp_path / "knowledge/notes"
    notes.mkdir(parents=True)
    page = notes / "page.md"
    content = b"x" * (4 * 1024 * 1024 + 1)
    page.write_bytes(content)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
    result = mcp_server.read_page("page", emit_telemetry=False, resolve_evidence=False)
    assert result["content"].encode() == content
    assert mcp_server.file_sha256(page) == hashlib.sha256(content).hexdigest()
