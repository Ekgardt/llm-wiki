"""A tool call is validated, bounded by its deadline and answered with one safe envelope."""
from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta

import pytest

from tests.mcp_server_support import (
    ENVELOPE_FIELDS,
    MISSING_REQUIRED_CALLS,
    TOOL_HELPERS,
    VALID_TOOL_CALLS,
    WRONG_TYPE_CALLS,
    ToolCallHarness,
    _assert_abstention_data,
    _assert_abstention_quality,
    _assert_bm25_fallback,
    _assert_bounded_error_text,
    _assert_degraded_envelope,
    _assert_error_degrades_envelope,
    _assert_grounded_answer_envelope,
    _assert_grounded_answer_quality,
    _assert_grounded_call,
    _assert_helper_failure_data,
    _assert_low_confidence_envelope,
    _assert_no_index_freshness_claim,
    _assert_no_sensitive_text,
    _named_generations,
)


class TestHandleToolCall(ToolCallHarness):
    """Envelope, validation, recall, search quality and doctor answers of a tool call."""

    def test_recall_returns_json(self):
        data = self._data("recall", {"query": "auth"})
        assert isinstance(data["results"], list)
        assert set(data["retrieval_trace"]) == {
            "schema_version", "requested_mode", "effective_mode", "signals_used",
            "fallback_reason", "corpus_generation", "partial", "reranker_applied",
            "reranker_model_id", "reranker_model_revision", "reranker_depth",
            "reranker_duration_ms", "reranker_fallback_reason", "signals_requested",
        }
        assert "_meta" in data

    @pytest.mark.parametrize("arguments", [{"query": "auth"}, {"query": "auth", "grounded": False}])
    def test_recall_defaults_to_current_search(self, monkeypatch, arguments):
        import query_memory

        searches = []
        monkeypatch.setattr(
            "llm_wiki.mcp_server_vault.search_vault",
            lambda query, *, limit, trace_sink=None: searches.append((query, limit)) or [],
        )
        monkeypatch.setattr(
            query_memory,
            "grounded_qa",
            lambda *args, **kwargs: pytest.fail("grounded QA must be opt-in"),
        )
        monkeypatch.setattr("llm_wiki.mcp_server_validation.meta", lambda: {})

        envelope = json.loads(self._run("recall", arguments))

        assert searches == [("auth", 8)]
        assert "results" in envelope["data"]

    def test_grounded_recall_calls_direct_api_with_root_profile_and_mcp_deadline(
        self, monkeypatch, tmp_path
    ):
        import query_memory

        answer = {
            "schema_version": "grounded-answer/v1",
            "status": "answered",
            "claims": [{"text": "Alpha is enabled.", "citation_ids": ["E1"]}],
            "citations": [{"citation_id": "E1"}],
            "reason": None,
        }
        calls = []

        def grounded_qa(question, **kwargs):
            calls.append((question, kwargs))
            return answer

        monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
        monkeypatch.setattr(query_memory, "grounded_qa", grounded_qa)
        monkeypatch.setattr(
            query_memory,
            "answer",
            lambda *args, **kwargs: pytest.fail("CLI answer wrapper must not be used"),
        )
        monkeypatch.setattr(
            "llm_wiki.mcp_server_vault.search_vault",
            lambda *args, **kwargs: pytest.fail("search response must not wrap grounded QA"),
        )
        started = time.monotonic()

        envelope = json.loads(
            self._run(
                "recall",
                {"query": "Is alpha enabled?", "grounded": True, "profile": "EXACT"},
            )
        )

        _assert_grounded_answer_envelope(envelope, answer)
        _assert_grounded_call(calls[0], tmp_path, started)
        _assert_grounded_answer_quality(envelope)

    @pytest.mark.parametrize(
        "status",
        ["insufficient_evidence", "conflicting_evidence", "unsupported_time_scope"],
    )
    def test_grounded_recall_abstention_is_valid_partial_with_reason_warning(
        self, monkeypatch, status
    ):
        import query_memory

        reason = f"Cannot answer because status is {status}."
        monkeypatch.setattr(
            query_memory,
            "grounded_qa",
            lambda *args, **kwargs: {
                "schema_version": "grounded-answer/v1",
                "status": status,
                "claims": [],
                "citations": [],
                "reason": reason,
            },
        )

        envelope = json.loads(
            self._run("recall", {"query": "question", "grounded": True})
        )

        _assert_abstention_data(envelope, status)
        _assert_abstention_quality(envelope, reason)

    @pytest.mark.parametrize(
        "sensitive",
        [
            "api_key=sk-abcdefghijklmnopqrstuvwxyz123456",
            r"C:\Users\operator\private\vault\knowledge\notes\secret.md",
            "PROMPT_SENTINEL do not reveal this user question",
        ],
    )
    def test_grounded_recall_exception_text_is_safe_and_bounded(
        self, monkeypatch, sensitive
    ):
        import query_memory

        def fail(*args, **kwargs):
            raise query_memory.GroundedQAError((sensitive + " ") * 100)

        monkeypatch.setattr(query_memory, "grounded_qa", fail)

        envelope = json.loads(
            self._run("recall", {"query": "question", "grounded": True})
        )

        error = envelope["data"]["error"]
        _assert_bounded_error_text(envelope, error, sensitive)
        _assert_error_degrades_envelope(envelope, error)

    @pytest.mark.parametrize("failure", ["provider failed", "schema validation failed"])
    def test_grounded_recall_provider_and_schema_failures_use_error_envelope(
        self, monkeypatch, failure
    ):
        import query_memory

        monkeypatch.setattr(
            query_memory,
            "grounded_qa",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                query_memory.GroundedQAError(failure)
            ),
        )

        envelope = json.loads(
            self._run("recall", {"query": "question", "grounded": True})
        )

        assert "error" in envelope["data"]
        assert failure not in json.dumps(envelope)
        assert envelope["partial"] is True

    def test_final_envelope_sanitizes_unknown_name_and_helper_diagnostics(self):
        import mcp_server

        sensitive = r"api_key=sk-abcdefghijklmnopqrstuvwxyz C:\private\vault\secret.md"
        unknown = json.loads(
            mcp_server.execute_tool_call(sensitive, {}, time.monotonic() + 1)
        )
        helper = mcp_server.build_operation_envelope(
            {"nested": {"error": f"failed at {sensitive}"}},
            {"warnings": [f"warning from {sensitive}"]},
        )

        for envelope in (unknown, helper):
            _assert_no_sensitive_text(envelope, sensitive)
        assert "Unknown tool:" in unknown["data"]["error"]
        assert "failed at" in helper["data"]["nested"]["error"]

    def test_a_failed_tool_call_records_which_tool_failed(self, monkeypatch):
        import capture_diagnostics
        import mcp_server

        recorded: list[tuple[str, str]] = []
        monkeypatch.setattr(
            capture_diagnostics,
            "record_capture_failure",
            lambda kind, reason, **_options: recorded.append((kind, reason)),
        )

        def explode(*_args, **_options):
            raise RuntimeError("page read exploded")

        monkeypatch.setattr("llm_wiki.mcp_server_vault.read_page", explode)
        envelope = json.loads(
            mcp_server.execute_tool_call(
                "read_page", {"slug": "any-page"}, time.monotonic() + 5
            )
        )

        assert envelope["data"]["error"] == "operation_failed"
        assert recorded, "the failure left no diagnostic trace at all"
        kind, reason = recorded[-1]
        assert kind == "mcp_tool"
        assert reason.startswith("mcp.read_page:"), reason

    def test_recall_exposes_validated_planner_trace_and_component_generation(self, monkeypatch):

        row = {
            "path": "knowledge/notes/auth.md",
            "requested_mode": "HYBRID",
            "effective_mode": "BASE",
            "signals_used": ["lexical"],
            "fallback_reason": "dense_unavailable",
            "generation": "gen-17",
            "reranker_applied": False,
            "reranker_model_id": None,
            "reranker_model_revision": None,
            "reranker_depth": None,
            "reranker_duration_ms": None,
            "reranker_fallback_reason": None,
        }
        monkeypatch.setattr("llm_wiki.mcp_server_vault.search_vault", lambda *args, **kwargs: [row])
        monkeypatch.setattr("llm_wiki.mcp_server_validation.meta", lambda: {})

        envelope = json.loads(self._run("recall", {"query": "auth"}))

        trace = envelope["data"]["retrieval_trace"]
        assert trace["requested_mode"] == "HYBRID"
        assert trace["effective_mode"] == "BASE"
        assert trace["corpus_generation"] == "gen-17"
        assert envelope["components"]["lexical"] == {
            "generation": "gen-17", "freshness": "fresh"
        }

    def test_the_envelope_says_what_the_trace_says(self, monkeypatch):
        """Issue #26.1: rows and trace said partial and a timeout; the envelope said neither."""

        row = {
            "path": "knowledge/notes/auth.md",
            "fused_score": 1.0,
            "requested_mode": "HYBRID",
            "effective_mode": "BASE",
            "signals_used": ["lexical"],
            "fallback_reason": "optional_stage_timeout",
            "generation": "gen-17",
            "partial": True,
        }
        monkeypatch.setattr("llm_wiki.mcp_server_vault.search_vault", lambda *args, **kwargs: [row])
        monkeypatch.setattr("llm_wiki.mcp_server_validation.meta", lambda: {})

        envelope = json.loads(self._run("recall", {"query": "auth"}))

        assert envelope["partial"] is True
        assert envelope["fallback"] is True
        assert "Retrieval fell back: optional_stage_timeout." in envelope["warnings"]

    def test_empty_recall_reports_the_run_that_found_nothing(self, monkeypatch):
        """Issue #26.1: an empty result names the generation and the signals that ran."""

        def _empty_run(*args, trace_sink=None, **kwargs):
            trace_sink.update(
                {
                    "requested_mode": "HYBRID",
                    "effective_mode": "HYBRID",
                    "signals_used": ("lexical", "dense"),
                    "fallback_reason": None,
                    "corpus_generation": "gen-17",
                    "partial": False,
                }
            )
            return []

        monkeypatch.setattr("llm_wiki.mcp_server_vault.search_vault", _empty_run)
        monkeypatch.setattr("llm_wiki.mcp_server_validation.meta", lambda: {})

        envelope = json.loads(self._run("recall", {"query": "missing"}))

        trace = envelope["data"]["retrieval_trace"]
        components = envelope["components"]
        assert (
            trace["corpus_generation"],
            trace["signals_used"],
            trace["fallback_reason"],
            trace["partial"],
        ) == ("gen-17", ["lexical", "dense"], None, False)
        # HYBRID declares lexical and dense only, so graph is not reported at all.
        assert (
            components["dense"],
            "graph" in components,
            _named_generations(components),
        ) == ({"generation": "gen-17", "freshness": "fresh"}, False, {"gen-17"})

    def test_recall_rejects_trace_outside_the_closed_schema(self, monkeypatch):

        row = {
            "requested_mode": "HYBRID",
            "effective_mode": "invented",
            "signals_used": ["lexical"],
            "generation": "gen-17",
        }
        monkeypatch.setattr("llm_wiki.mcp_server_vault.search_vault", lambda *args, **kwargs: [row])

        envelope = json.loads(self._run("recall", {"query": "auth"}))

        assert "error" in envelope["data"]

    def test_wiki_overview_returns_json(self):
        data = self._data("wiki_overview", {})
        assert "page_count" in data
        assert "_meta" in data

    def test_read_page_returns_json(self):
        data = self._data("read_page", {"slug": "nonexistent-xxx"})
        assert "error" in data

    def test_unknown_tool_returns_error(self):
        envelope = json.loads(self._run("nonexistent_tool", {}))
        assert "error" in envelope["data"]
        _assert_degraded_envelope(envelope)

    def test_compile_returns_json(self, monkeypatch):
        import compile_memory

        monkeypatch.setattr(compile_memory, "run_pending_compile", lambda **kwargs: 0)
        data = self._data("compile", {})
        assert data == {"status": "completed", "returncode": 0}

    @pytest.mark.parametrize("tool_name", VALID_TOOL_CALLS)
    def test_every_helper_exception_returns_degraded_envelope(self, monkeypatch, tool_name):

        def fail(*args, **kwargs):
            raise RuntimeError(f"{tool_name} failed")

        monkeypatch.setattr(TOOL_HELPERS[tool_name], fail)

        envelope = json.loads(self._run(tool_name, VALID_TOOL_CALLS[tool_name]))

        _assert_helper_failure_data(envelope, tool_name)
        _assert_degraded_envelope(envelope)

    @pytest.mark.parametrize(
        "tool_name",
        ["read_page", "log_decision", "find_dead_code", "get_architecture"],
    )
    def test_helper_error_payloads_are_degraded(self, monkeypatch, tool_name):

        monkeypatch.setattr(TOOL_HELPERS[tool_name],
            lambda *args, **kwargs: {"error": "helper error"},
        )

        envelope = json.loads(self._run(tool_name, VALID_TOOL_CALLS[tool_name]))

        assert envelope["data"] == {"error": "helper error"}
        assert envelope["partial"] is True
        assert envelope["coverage"] == 0
        assert envelope["confidence"] < 1

    @pytest.mark.parametrize("tool_name, arguments", MISSING_REQUIRED_CALLS)
    def test_missing_required_inputs_are_enveloped_before_dispatch(
        self, monkeypatch, tool_name, arguments
    ):

        called = False

        def helper(*args, **kwargs):
            nonlocal called
            called = True

        monkeypatch.setattr(TOOL_HELPERS[tool_name], helper)

        envelope = json.loads(self._run(tool_name, arguments))

        assert "required" in envelope["data"]["error"].lower()
        assert envelope["partial"] is True
        assert called is False

    @pytest.mark.parametrize("tool_name, arguments", WRONG_TYPE_CALLS)
    def test_wrong_input_types_and_bounds_are_enveloped_before_dispatch(
        self, monkeypatch, tool_name, arguments
    ):

        called = False

        def helper(*args, **kwargs):
            nonlocal called
            called = True

        monkeypatch.setattr(TOOL_HELPERS[tool_name], helper)

        envelope = json.loads(self._run(tool_name, arguments))

        assert "error" in envelope["data"]
        assert envelope["partial"] is True
        assert called is False

    @pytest.mark.parametrize(
        ("tool_name", "arguments"),
        [
            ("recall", {"query": 42}),
            ("get_decisions", {"query": True}),
            ("read_page", {"slug": "x" * 256}),
            ("get_context", {"slugs": []}),
            ("get_context", {"slugs": ["page"], "include": [True]}),
            ("get_context", {"slugs": ["same", "same"]}),
        ],
    )
    def test_retrieval_bounds_are_rejected_before_helper_dispatch(
        self, monkeypatch, tool_name, arguments
    ):

        called = False

        def helper(*args, **kwargs):
            nonlocal called
            called = True

        monkeypatch.setattr(TOOL_HELPERS[tool_name], helper)
        envelope = json.loads(self._run(tool_name, arguments))

        assert "error" in envelope["data"]
        assert called is False

    @pytest.mark.parametrize("arguments", [None, [], "query", 1, True])
    def test_non_object_arguments_are_enveloped(self, arguments):
        envelope = json.loads(self._run("wiki_overview", arguments))

        assert "object" in envelope["data"]["error"].lower()
        assert envelope["partial"] is True

    def test_every_tool_success_path_uses_the_envelope(self, monkeypatch):

        monkeypatch.setattr("llm_wiki.mcp_server_vault.search_vault", lambda *args, **kwargs: [])
        monkeypatch.setattr("llm_wiki.mcp_server_vault.read_page", lambda *args, **kwargs: {"ok": True})
        monkeypatch.setattr("llm_wiki.mcp_server_vault.wiki_overview", lambda: {"ok": True})
        monkeypatch.setattr("llm_wiki.mcp_server_vault.vault_status", lambda: {"ok": True})
        monkeypatch.setattr("llm_wiki.mcp_server_vault.get_decisions", lambda *args, **kwargs: [])
        monkeypatch.setattr("llm_wiki.mcp_server_vault.get_context", lambda *args, **kwargs: {})
        monkeypatch.setattr("llm_wiki.mcp_server_vault.check_contradiction", lambda *args: [])
        monkeypatch.setattr("llm_wiki.mcp_server_vault.log_decision", lambda *args: {"ok": True})
        monkeypatch.setattr("llm_wiki.mcp_server_vault.trigger_compile", lambda: {"ok": True})
        monkeypatch.setattr("llm_wiki.mcp_server_architecture.find_dead_code_answer", lambda *args: {"ok": True})
        monkeypatch.setattr("llm_wiki.mcp_server_architecture.architecture_answer", lambda *args: {"ok": True})
        monkeypatch.setattr("llm_wiki.mcp_server_doctor.doctor", lambda **kwargs: {"overall_status": "ok"})
        for name, arguments in VALID_TOOL_CALLS.items():
            envelope = json.loads(self._run(name, arguments))
            assert set(envelope) == ENVELOPE_FIELDS, name

    @pytest.mark.parametrize(
        "tool_name, helper_name",
        [
            ("recall", "search_vault"),
            ("get_decisions", "get_decisions"),
            ("check_contradiction", "check_contradiction"),
        ],
    )
    def test_bm25_only_search_is_marked_as_fallback(
        self, monkeypatch, tool_name, helper_name
    ):

        monkeypatch.setattr(
            f"llm_wiki.mcp_server_vault.{helper_name}",
            lambda *args, **kwargs: [{"path": "page.md", "score": 1.0}],
        )

        envelope = json.loads(self._run(tool_name, VALID_TOOL_CALLS[tool_name]))

        _assert_bm25_fallback(envelope)
        _assert_low_confidence_envelope(envelope)

    @pytest.mark.parametrize("tool_name", ["recall", "get_decisions", "check_contradiction"])
    def test_empty_search_results_have_low_coverage(self, monkeypatch, tool_name):

        monkeypatch.setattr(TOOL_HELPERS[tool_name], lambda *args, **kwargs: []
        )

        envelope = json.loads(self._run(tool_name, VALID_TOOL_CALLS[tool_name]))

        assert envelope["partial"] is True
        assert envelope["coverage"] <= 0.25
        assert envelope["confidence"] < 0.5
        assert envelope["warnings"]

    def test_fused_search_does_not_claim_full_certainty(self, monkeypatch):

        monkeypatch.setattr(
            "llm_wiki.mcp_server_vault.search_vault",
            lambda *args, **kwargs: [{"path": "page.md", "fused_score": 0.1}],
        )

        envelope = json.loads(self._run("recall", {"query": "test"}))

        assert envelope["fallback"] is False
        assert envelope["coverage"] < 1
        assert envelope["confidence"] < 1

    @pytest.mark.parametrize(
        "limit, effective",
        [(-10, 1), (0, 1), (21, 20), (10_000, 20)],
    )
    def test_recall_accepts_integer_limit_but_clamps_helper(
        self, monkeypatch, limit, effective
    ):

        received = []

        def search(query, *, limit, trace_sink=None):
            received.append(limit)
            return [{"path": "page.md", "fused_score": 0.1}]

        monkeypatch.setattr("llm_wiki.mcp_server_vault.search_vault", search)

        envelope = json.loads(
            self._run("recall", {"query": "test", "limit": limit})
        )

        assert "error" not in envelope["data"]
        assert received == [effective]
        assert envelope["partial"] is True
        assert any("clamp" in warning.lower() for warning in envelope["warnings"])

    @pytest.mark.parametrize("limit, effective", [(-1, 1), (0, 1), (21, 20), (999, 20)])
    def test_get_decisions_accepts_integer_limit_but_clamps_helper(
        self, monkeypatch, limit, effective
    ):

        received = []

        def decisions(query, *, limit, trace_sink=None):
            received.append(limit)
            return [{"path": "decision.md", "fused_score": 0.1}]

        monkeypatch.setattr("llm_wiki.mcp_server_vault.get_decisions", decisions)

        envelope = json.loads(self._run("get_decisions", {"limit": limit}))

        assert "error" not in envelope["data"]
        assert received == [effective]
        assert envelope["partial"] is True
        assert any("clamp" in warning.lower() for warning in envelope["warnings"])

    @pytest.mark.parametrize("tool_name", ["recall", "get_decisions", "check_contradiction"])
    @pytest.mark.parametrize("index_state", ["stale", "missing"])
    def test_legacy_index_age_does_not_claim_component_freshness(
        self, monkeypatch, tmp_path, tool_name, index_state
    ):
        import mcp_contract

        if index_state == "stale":
            index = tmp_path / "cache" / "index.sqlite"
            index.parent.mkdir()
            index.touch()
            old = datetime.now(UTC) - timedelta(days=2)
            os.utime(index, (old.timestamp(), old.timestamp()))
        real_build = mcp_contract.build_envelope
        monkeypatch.setattr(
            "llm_wiki.mcp_server_quality.build_envelope",
            lambda data, **kwargs: real_build(data, root=tmp_path, **kwargs),
        )
        monkeypatch.setattr(TOOL_HELPERS[tool_name],
            lambda *args, **kwargs: [{"path": "page.md", "fused_score": 0.1}],
        )
        monkeypatch.setattr("llm_wiki.mcp_server_validation.meta", lambda: {})

        envelope = json.loads(self._run(tool_name, VALID_TOOL_CALLS[tool_name]))

        _assert_no_index_freshness_claim(envelope)
        if tool_name != "recall":
            assert envelope["freshness"] == "unknown"

    def test_get_context_accepts_unrecognized_include_strings(self, monkeypatch):

        monkeypatch.setattr(
            "llm_wiki.mcp_server_vault.get_context",
            lambda slugs, include: {"include": include},
        )

        envelope = json.loads(
            self._run(
                "get_context", {"slugs": ["missing"], "include": ["future-option"]}
            )
        )

        assert envelope["data"]["include"] == ["future-option"]

    def test_contradiction_candidates_are_always_unverified(self, monkeypatch):

        monkeypatch.setattr(
            "llm_wiki.mcp_server_vault.check_contradiction",
            lambda claim: [{"path": "page.md", "fused_score": 0.1}],
        )

        envelope = json.loads(
            self._run("check_contradiction", {"claim": "conflicting claim"})
        )

        assert envelope["partial"] is True
        assert envelope["confidence"] < 0.6
        assert any("unverified" in warning.lower() for warning in envelope["warnings"])

    def test_tool_text_is_strict_json_for_non_finite_helper_data(self, monkeypatch):

        monkeypatch.setattr(
            "llm_wiki.mcp_server_vault.wiki_overview", lambda: {"score": float("nan")}
        )
        monkeypatch.setattr("llm_wiki.mcp_server_validation.meta", lambda: {})

        text = self._run("wiki_overview", {})

        assert "NaN" not in text
        assert json.loads(text)["data"]["score"] is None
