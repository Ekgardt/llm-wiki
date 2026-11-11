"""Shared fixtures, call tables and assertions for the MCP server tests."""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

from llm_wiki import mcp_server_limits, mcp_server_navigation

from tests.slow_machine import SHORT_TIMEOUT

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


# The property is that a blocked daemon worker never joins at exit, so the
# interpreter leaves at once instead of waiting for it. Half a second measured
# that on an idle machine and failed at 0.551 s on a loaded one, which is the
# machine and not the property. The outer bound stays larger than the inner one
# so a process that truly hangs still fails as a hang.
_SHUTDOWN_SECONDS = 2.0


_HUNG_PROCESS_SECONDS = 5.0


class ToolCallHarness:
    """Runs one MCP tool call to completion and opens its envelope."""

    _MISSING = object()

    def _run(self, name, args=_MISSING):
        from mcp_server import handle_tool_call
        if args is self._MISSING:
            args = {}
        return asyncio.run(handle_tool_call(name, args))

    def _data(self, name, args=None):
        envelope = json.loads(self._run(name, args))
        assert set(envelope) == ENVELOPE_FIELDS
        return envelope["data"]


def _named_generations(components: dict) -> set:
    """Every generation the envelope's components name."""
    return {detail["generation"] for detail in components.values()}


def _warned_about(envelope: dict, needle: str) -> bool:
    return any(needle in warning for warning in envelope["warnings"])


def _expected_task_state(action: str) -> str:
    if action == "queue-cancel":
        return "ready"
    return "dead"


def _drain_mcp_workers(seconds: float = 30.0) -> None:
    deadline = time.monotonic() + seconds
    while mcp_server_limits.MCP_WORKERS and time.monotonic() < deadline:
        time.sleep(0.01)


def _redrive_links(queue) -> list:
    return [task.redrive_of for task in queue.list_tasks() if task.redrive_of]


def _canonical_architecture_modes(mcp_server) -> set:
    """Every mode a call can actually reach.

    A structural mode reaches a handler only when `ARCHITECTURE_CONTRACTS`
    names its arguments, because `_architecture_contract` indexes that dict;
    a precise mode is served through `PRECISE_ARCHITECTURE_MODES` instead.
    Those two sets are the product's own answer to "what does this tool serve",
    which is why the schema is compared against them and not against a copy.
    """
    return set(mcp_server.ARCHITECTURE_CONTRACTS) | set(
        mcp_server.PRECISE_ARCHITECTURE_MODES
    )


ENVELOPE_FIELDS = {
    "schema_version",
    "generated_at",
    "index_timestamp",
    "source_commit",
    "freshness",
    "coverage",
    "confidence",
    "fallback",
    "partial",
    "warnings",
    "components",
    "data",
}


VALID_TOOL_CALLS = {
    "recall": {"query": "test"},
    "read_page": {"slug": "test"},
    "wiki_overview": {},
    "vault_status": {},
    "get_decisions": {},
    "get_context": {"slugs": ["missing"]},
    "check_contradiction": {"claim": "test"},
    "log_decision": {"summary": "test"},
    "compile": {},
    "find_dead_code": {"directory": "C:\\project"},
    "get_architecture": {"directory": "C:\\project"},
    "doctor": {"action": "status"},
}


TOOL_HELPERS = {
    "recall": "llm_wiki.mcp_server_vault.search_vault",
    "read_page": "llm_wiki.mcp_server_vault.read_page",
    "wiki_overview": "llm_wiki.mcp_server_vault.wiki_overview",
    "vault_status": "llm_wiki.mcp_server_vault.vault_status",
    "get_decisions": "llm_wiki.mcp_server_vault.get_decisions",
    "get_context": "llm_wiki.mcp_server_vault.get_context",
    "check_contradiction": "llm_wiki.mcp_server_vault.check_contradiction",
    "log_decision": "llm_wiki.mcp_server_vault.log_decision",
    "compile": "llm_wiki.mcp_server_vault.trigger_compile",
    "find_dead_code": "llm_wiki.mcp_server_architecture.find_dead_code_answer",
    "get_architecture": "llm_wiki.mcp_server_architecture.architecture_answer",
    "doctor": "llm_wiki.mcp_server_doctor.doctor",
}


class DrainAwareSet(set[object]):
    """Signal when a test-owned MCP worker set becomes empty."""

    def __init__(self, drained: threading.Event) -> None:
        super().__init__()
        self._drained = drained

    def discard(self, item: object) -> None:
        super().discard(item)
        if not self:
            self._drained.set()


MISSING_REQUIRED_CALLS = [
    ("recall", {}),
    ("read_page", {}),
    ("get_context", {}),
    ("check_contradiction", {}),
    ("log_decision", {}),
    ("find_dead_code", {}),
    ("get_architecture", {}),
]


WRONG_TYPE_CALLS = [
    ("recall", {"query": 1}),
    ("recall", {"query": "x", "limit": True}),
    ("read_page", {"slug": []}),
    ("get_decisions", {"query": 1}),
    ("get_decisions", {"limit": "10"}),
    ("get_context", {"slugs": "page"}),
    ("get_context", {"slugs": [1]}),
    ("get_context", {"slugs": [], "include": "frontmatter"}),
    ("get_context", {"slugs": [], "include": [1]}),
    ("check_contradiction", {"claim": 1}),
    ("log_decision", {"summary": 1}),
    ("log_decision", {"summary": "x", "rationale": 1}),
    ("find_dead_code", {"directory": 1}),
    ("get_architecture", {"directory": 1}),
    ("doctor", {"action": "status", "repair": True}),
]


def _assert_task_shaped_names(names) -> None:
    for expected in (
        "recall",
        "read_page",
        "wiki_overview",
        "vault_status",
        "compile",
        "find_dead_code",
        "get_architecture",
        "doctor",
    ):
        assert expected in names


def _assert_doctor_branch_shape(schema) -> None:
    assert set(schema) == {"type", "oneOf"}
    assert schema["type"] == "object"
    assert len(schema["oneOf"]) == 9


def _assert_doctor_branch_actions(branches) -> None:
    assert all(branch["additionalProperties"] is False for branch in branches)
    assert {branch["properties"]["action"]["const"] for branch in branches} == {
        "status",
        "queue-inspect",
        "queue-cancel",
        "queue-redrive",
        "queue-dead-list",
        "transaction-recover",
        "transaction-undo",
        "archive-status",
        "claim-status",
    }


def _assert_directory_is_the_only_requirement(tools, name: str) -> None:
    tool = next(item for item in tools if item.name == name)
    assert tool.inputSchema["required"] == ["directory"]
    assert tool.inputSchema["properties"]["directory"]["type"] == "string"


def _assert_query_and_slug_bounds(schemas) -> None:
    assert "maxLength" not in schemas["recall"]["properties"]["query"]
    assert "maxLength" not in schemas["get_decisions"]["properties"]["query"]
    assert schemas["read_page"]["properties"]["slug"]["maxLength"] == 255


def _assert_context_array_bounds(slugs, include) -> None:
    assert (slugs["minItems"], slugs["uniqueItems"]) == (1, True)
    assert "maxItems" not in slugs
    assert slugs["items"]["maxLength"] == 255
    assert include["uniqueItems"] is True
    assert "maxItems" not in include
    assert include["items"] == {"type": "string"}


def _assert_unsupported_evidence_result(result) -> None:
    assert result["validity"]["status"] == "unsupported-evidence"
    assert result["recommendations"] == ["quarantine"]
    assert result["evidence"][0]["retrieval_only"] is True


def _assert_unsupported_evidence_quality(quality) -> None:
    assert quality["partial"] is True
    assert any("unsupported" in item.lower() for item in quality["warnings"])


def _assert_vault_status_tool_description(tools) -> None:
    tool = next(item for item in tools if item.name == "vault_status")
    assert tool.description == (
        "Get compile status and current daily-file backlog."
    )


def _assert_redacted_failure(result, sensitive: str) -> None:
    encoded = json.dumps(result)
    assert sensitive not in encoded
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in encoded
    assert r"C:\private\vault" not in encoded
    assert result["error"].endswith("operation_failed")


def _assert_redacted_error_result(result, sensitive: str) -> None:
    encoded = json.dumps(result)
    assert sensitive not in encoded
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in encoded
    assert r"C:\private\vault" not in encoded
    assert result == {"error": "operation_failed"}


def _assert_degraded_search_calls(calls) -> None:
    assert len(calls) == 2
    assert calls[0][1]["semantic"] is True
    assert calls[1][1]["semantic"] is False
    assert calls[1][1]["graph"] is False


def _assert_degraded_search_budget(calls) -> None:
    import mcp_server

    reserve = mcp_server.LEXICAL_FALLBACK_RESERVE_SECONDS
    assert (calls[1][1]["rerank"], calls[1][1]["deadline_monotonic"] - calls[0][1]["deadline_monotonic"]) == (
        False, reserve,
    )


def _assert_degraded_result_row(row) -> None:
    assert row["requested_mode"] == "HYBRID"
    assert row["effective_mode"] == "BASE"
    assert row["signals_used"] == ["lexical"]
    assert row["fallback_reason"] == "retrieval_deadline_exceeded"


def _assert_degraded_trace(row, trace) -> None:
    assert row["partial"] is True
    assert trace["fallback_reason"] == "retrieval_deadline_exceeded"
    assert trace["partial"] is True


def _assert_context_package_keys(package) -> None:
    assert set(package) >= {"text", "packed_tokens", "token_budget", "repo_map", "items", "dropped"}


def _assert_context_package_contents(package) -> None:
    assert package["packed_tokens"] <= package["token_budget"] == 1200
    assert {item["type"] for item in package["items"]} >= {"decision", "debugging", "project-state"}


def _assert_recall_search_call(call) -> None:
    assert call[1]["source_tool"] == "mcp.recall"
    assert call[1]["semantic"] is True
    assert call[1]["deadline_monotonic"] > time.monotonic()


def _assert_decisions_search_call(call) -> None:
    assert call[1]["source_tool"] == "mcp.get_decisions"
    assert call[1]["emit_telemetry"] is False


def _assert_decision_filter_results(results) -> None:
    assert [Path(result["path"]).stem for result in results] == [
        "first-decision", "second-decision"
    ]


def _assert_decision_filter_rows(rows) -> None:
    # One column, one identity: the page's vault-relative path. See
    # `docs/research/2026-09-17-one-page-one-identity-and-one-set-of-windows.md`.
    assert [(row.candidate_id, row.rank) for row in rows] == [
        ("knowledge/notes/first-decision.md", 1),
        ("knowledge/notes/second-decision.md", 2),
    ]
    assert all(row.retrieval_mode == "decision-filter" for row in rows)
    assert all(row.source_tool == "mcp.get_decisions" for row in rows)


def _assert_read_page_events(rows) -> None:
    assert {(row.event_kind, row.source_tool) for row in rows} == {
        ("page_read", "mcp.read_page"),
        ("evidence_read", "mcp.read_page"),
    }
    # A page read names the page the way every other writer of this column
    # does — its vault-relative path, not the slug two pages can share.
    read_event = next(row for row in rows if row.event_kind == "page_read")
    assert read_event.candidate_id == "knowledge/notes/page.md"


def _assert_evidence_event(rows, expected_id: str) -> None:
    evidence_event = next(row for row in rows if row.event_kind == "evidence_read")
    assert evidence_event.candidate_id == expected_id


def _assert_dead_code_report(dead) -> None:
    assert dead["source_generation"] == "gen-25"
    assert dead["unresolved_count"] == 2
    assert dead["candidates"] == [{"name": "unused"}]


def _assert_architecture_report(architecture) -> None:
    assert architecture["source_generation"] == "gen-25"
    assert architecture["architecture"]["graph_complete"] is False


def _assert_grounded_call(call, vault, started) -> None:
    assert call[0] == "Is alpha enabled?"
    assert call[1]["vault"] == vault
    assert call[1]["profile"] == "EXACT"
    assert call[1]["deadline"] > started


def _assert_grounded_answer_envelope(envelope, answer) -> None:
    assert envelope["data"] == answer
    assert envelope["partial"] is False
    assert all("abstain" not in warning.lower() for warning in envelope["warnings"])


def _assert_grounded_answer_quality(envelope) -> None:
    # Coverage is the share of claims whose citations all came back, and the
    # warnings name a page or say nothing at all.
    assert envelope["coverage"] == 1.0
    assert 0 < envelope["confidence"] <= 0.8
    assert not any("unknown" in warning.lower() for warning in envelope["warnings"])
    assert envelope["components"] == {}


def _assert_abstention_data(envelope, status) -> None:
    assert envelope["data"]["status"] == status
    assert "error" not in envelope["data"]
    assert envelope["partial"] is True


def _assert_abstention_quality(envelope, reason) -> None:
    assert envelope["coverage"] == 0
    assert envelope["confidence"] == 0
    assert reason in envelope["warnings"]
    assert envelope["components"] == {}


def _assert_bounded_error_text(envelope, error, sensitive) -> None:
    assert sensitive not in json.dumps(envelope)
    assert len(error) <= 256
    assert all(len(warning) <= 256 for warning in envelope["warnings"])


def _assert_error_degrades_envelope(envelope, error) -> None:
    assert envelope["partial"] is True
    assert envelope["coverage"] == 0
    assert error in envelope["warnings"]


def _assert_timed_out_envelope(envelope, loop_progressed) -> None:
    assert loop_progressed is True
    assert envelope["data"] == {"error": "operation_timeout"}
    assert envelope["partial"] is True
    assert envelope["coverage"] == 0


def _assert_clean_shutdown(process, stdout, stderr, shutdown_started) -> None:
    assert process.returncode == 0, stderr
    assert stdout == ""
    assert time.perf_counter() - shutdown_started < _SHUTDOWN_SECONDS


def _assert_no_sensitive_text(envelope, sensitive: str) -> None:
    encoded = json.dumps(envelope)
    assert sensitive not in encoded
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in encoded
    assert r"C:\private\vault" not in encoded


def _assert_operation_timed_out(text) -> None:
    assert json.loads(text)["data"] == {"error": "operation_timeout"}


def _assert_timed_out_queue_call(text, commit_reached) -> None:
    assert commit_reached.is_set()
    _assert_operation_timed_out(text)


def _assert_queue_unchanged_after_timeout(queue, task_id, action) -> None:
    assert not mcp_server_limits.MCP_WORKERS
    assert queue.get(task_id).state == _expected_task_state(action)
    assert _redrive_links(queue) == []


def _assert_daily_untouched_after_timeout(daily) -> None:
    assert not mcp_server_limits.MCP_WORKERS
    assert daily.read_bytes() == b"# existing\n"


def _assert_late_failure_stderr(stderr: str, sensitive: str) -> None:
    assert "RuntimeError" in stderr
    assert sensitive not in stderr
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in stderr


def _assert_graph_mode_dispatch(data, calls, mode, result_key) -> None:
    assert data["mode"] == mode
    assert result_key in data["architecture"]
    assert calls
    assert calls[0][1]["with_report"] is True


def _assert_no_foreign_generation(envelope) -> None:
    assert envelope["data"]["source_generation"] is None
    assert envelope["data"]["fallback"] is True


def _assert_no_foreign_paths(envelope, repository_a, repository_b) -> None:
    encoded = json.dumps(envelope)
    assert "a_only_symbol" not in encoded
    assert str(repository_a) not in encoded
    assert str(repository_b / "app.py") not in encoded


def _assert_degraded_envelope(envelope) -> None:
    assert envelope["partial"] is True
    assert envelope["coverage"] == 0
    assert envelope["confidence"] < 1
    assert envelope["warnings"]


def _assert_low_confidence_envelope(envelope) -> None:
    assert envelope["partial"] is True
    assert envelope["coverage"] < 1
    assert envelope["confidence"] < 1
    assert envelope["warnings"]


def _assert_helper_failure_data(envelope, tool_name) -> None:
    assert "error" in envelope["data"]
    assert f"{tool_name} failed" not in json.dumps(envelope)
    assert len(envelope["data"]["error"]) <= 256


def _assert_bm25_fallback(envelope) -> None:
    assert envelope["fallback"] is True
    assert any("bm25" in warning.lower() for warning in envelope["warnings"])


def _assert_no_index_freshness_claim(envelope) -> None:
    assert envelope["index_timestamp"] is None
    assert not any(
        "index freshness" in warning.lower() for warning in envelope["warnings"]
    )


def _assert_summary_keeps_counts_and_names_the_mode(
    envelope, architecture: dict, directory
) -> None:
    """Only the member listing goes; the counts and every other field stay."""
    import mcp_server

    answered = envelope["data"]["architecture"]
    assert envelope["data"]["directory"] == str(directory.resolve())
    assert answered["communities"] == mcp_server.COMMUNITY_MEMBERS_HINT
    assert "mode=community" in answered["communities"]
    assert _without("communities", answered) == _without("communities", architecture)


def _replace_in_every_server_part(monkeypatch, name: str, value) -> None:
    """Replace one shared import, such as the clock, in every server part holding it."""
    import mcp_server  # noqa: F401  (loads every part)

    holders = [
        module
        for module_name, module in list(sys.modules.items())
        if module_name.startswith("llm_wiki.mcp_server_") and hasattr(module, name)
    ]
    assert holders, name
    for module in holders:
        monkeypatch.setattr(module, name, value)


def _without(dropped: str, mapping: dict) -> dict:
    return {key: value for key, value in mapping.items() if key != dropped}


def _assert_dead_code_verdict(data, verdict: str, names: list) -> None:
    assert data["verdict"] == verdict
    assert [row["name"] for row in data["candidates"]] == names
    assert data["candidate_count"] == 2


def _assert_code_tool_data(data, key, expected, directory) -> None:
    assert data[key] == expected
    assert data["directory"] == str(directory.resolve())


def _assert_doctor_status_envelope(envelope) -> None:
    assert set(envelope) == ENVELOPE_FIELDS
    assert envelope["data"]["overall_status"] == "degraded"
    assert envelope["partial"] is True
    assert envelope["confidence"] < 1


def _assert_rejected_queue_action(data, envelope, expected_code) -> None:
    assert data["overall_status"] == "error"
    assert data["codes"] == [expected_code]
    assert envelope["partial"] is True
    assert envelope["confidence"] < 0.5


def _assert_resource_timeout(envelope, loop_progressed, seen) -> None:
    assert loop_progressed is True
    assert len(seen) == 1
    assert envelope["data"] == {"error": "operation_timeout"}


def _assert_positioned_calls_are_precise(mcp_server) -> None:
    assert mcp_server.is_precise_architecture_request(
        {"mode": "definition", "path": "p.py", "line": 1, "character": 0}
    )
    assert mcp_server.is_precise_architecture_request(
        {"mode": "callers", "path": "p.py", "line": 1, "character": 0}
    )


def _assert_structural_calls_are_not_precise(mcp_server) -> None:
    assert not mcp_server.is_precise_architecture_request(
        {"mode": "callers", "symbol": "f"}
    )
    assert not mcp_server.is_precise_architecture_request({"mode": "summary"})
    assert not mcp_server.is_precise_architecture_request(
        {"mode": "callers", "path": "p.py", "line": 1}
    )


def _assert_failed_close_retains_owner(manager) -> None:
    assert mcp_server_navigation.NAVIGATION_MANAGER is None
    assert mcp_server_navigation.NAVIGATION_MANAGER_CLOSING is manager


def _assert_second_close_clears_owner(attempts: int) -> None:
    assert attempts == 2
    assert mcp_server_navigation.NAVIGATION_MANAGER is None
    assert mcp_server_navigation.NAVIGATION_MANAGER_CLOSING is None


def _assert_stale_worker_was_abandoned(stale_results, constructed) -> None:
    assert stale_results[0]["status"] == "timeout"
    assert constructed == []
    assert mcp_server_navigation.NAVIGATION_MANAGER is None


def _assert_fresh_manager_after_close(fresh, constructed) -> None:
    assert fresh["status"] == "ok"
    assert len(constructed) == 1
    assert mcp_server_navigation.NAVIGATION_MANAGER is constructed[0]


def _assert_precise_request_window(request, capability_name, direction) -> None:
    assert request.capability.name == capability_name
    assert request.direction == direction
    assert (request.offset, request.limit) == (7, 8)


def _assert_manager_request(manager_requests, deadline) -> None:
    assert len(manager_requests) == 1
    assert manager_requests[0][:2] == (deadline, 73)
    assert callable(manager_requests[0][2])
    assert manager_requests[0][2]() is False


def _assert_navigation_constructor(constructor, identity) -> None:
    assert constructor[2] is identity
    assert set(constructor[3]) == {
        "structural_candidates",
        "symbol_resolver",
        "edge_verifier",
    }
    assert all(callable(callback) for callback in constructor[3].values())


def _assert_single_render_window(captures, query_result) -> None:
    assert captures["renders"] == [query_result]
    assert len(captures["requests"]) == 1
    assert len(captures["renders"]) == 1


def _assert_precise_route_data(data, mode) -> None:
    assert data["mode"] == mode
    assert data["status"] == "ok"


def _assert_relative_graph_locations(items) -> None:
    assert all(not Path(item.path).is_absolute() for item in items)
    assert all(item.provenance[0].source == "graph" for item in items)


def _assert_bounded_source_reads(reads, deadline) -> None:
    assert reads
    assert all(max_bytes == 16 * 1024 * 1024 for _path, max_bytes, _deadline in reads)
    assert all(read_deadline == deadline for _path, _max, read_deadline in reads)


def _navigation_location_result(
    scope, span, *, source_kind="evidence", require_span_hash=True
):
    """Every case in the hash test passes the same metadata, version and budget."""
    from mcp_server import navigation_location_from_span

    return navigation_location_from_span(
        scope,
        span,
        source_kind=source_kind,
        require_span_hash=require_span_hash,
        metadata=None,
        graph_version="generation-1",
        deadline=time.monotonic() + SHORT_TIMEOUT,
    )


def _assert_mismatched_hashes_are_refused(scope, span) -> None:
    assert _navigation_location_result(
        scope, {**span, "source_sha256": "0" * 64}
    ) is None
    assert _navigation_location_result(
        scope, {**span, "span_sha256": "0" * 64}
    ) is None
    assert _navigation_location_result(
        scope, {**span, "span_sha256": "A" * 64}
    ) is None


def _assert_absent_span_hash_is_refused_only_when_required(scope, span) -> None:
    without = {key: value for key, value in span.items() if key != "span_sha256"}
    assert _navigation_location_result(scope, without) is None
    assert _navigation_location_result(
        scope, without, source_kind="occurrence", require_span_hash=False
    ) is not None


def _assert_the_cache_holds_the_newest_source(cache, reads: int) -> None:
    """The source past the cap evicted the older one instead of being refused."""
    assert reads == 2
    assert cache._bytes == 5
    assert list(key[2] for key in cache._values) == ["large.py"]


def _assert_read_only_graph_options(captured, scope, deadline) -> None:
    assert captured["root"] == Path(scope.checkout_root)
    assert captured["options"]["read_only"] is True
    assert captured["options"]["deadline"] == deadline
    assert callable(captured["options"]["cancelled"])


def _assert_normalized_error_data(data) -> None:
    assert data["status"] == "error"
    assert data["mode"] == "definition"
    assert "error" not in data
    assert set(data) >= {
        "freshness",
        "provider",
        "repository",
        "groups",
        "diagnostics",
        "warnings",
    }


def _assert_text_is_redacted(text: str, sensitive: str) -> None:
    assert sensitive not in text
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in text


def _assert_normalized_timeout_data(data) -> None:
    assert data["status"] == "timeout"
    assert data["mode"] == "references"
    assert data["requested_capability"] == "references"


def _assert_timeout_warning_only(data) -> None:
    assert data["warnings"] == ["navigation_timeout"]
    assert "error" not in data


def _assert_static_timeout_envelope(envelope) -> None:
    assert set(envelope) == ENVELOPE_FIELDS
    assert envelope["source_commit"] is None
    assert envelope["index_timestamp"] is None
    assert envelope["components"] == {}


def _assert_timeout_envelope_data(envelope) -> None:
    assert envelope["partial"] is True
    assert envelope["warnings"] == ["navigation_timeout"]
    assert envelope["data"]["status"] == "timeout"
    assert envelope["data"]["directory"] is None


def _assert_quality_without_mutation(quality, data, before, expected_partial) -> None:
    assert quality["partial"] is expected_partial
    assert quality["warnings"] == list(data["warnings"])
    assert data == before
    assert "_navigation_partial" not in data


def _assert_navigation_text_is_redacted(text: str, sensitive_path: str) -> None:
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in text
    assert sensitive_path not in text


def _assert_rendered_fields_survive_redaction(rendered) -> None:
    assert rendered["groups"][0]["locations"][0]["signature"]
    assert rendered["diagnostics"][0]["message"]
    assert rendered["diagnostics"][0]["code"]
    assert rendered["diagnostics"][0]["related"][0]["signature"]


def _assert_rendered_code_is_redacted(rendered, sensitive_path: str) -> None:
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in rendered["diagnostics"][0]["code"]
    assert sensitive_path not in rendered["diagnostics"][0]["code"]
