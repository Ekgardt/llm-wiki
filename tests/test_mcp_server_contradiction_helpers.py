"""The MCP contradiction helpers return a structured assessment and report unsupported evidence for a plain claim."""
from __future__ import annotations

from tests.mcp_server_support import (
    _assert_unsupported_evidence_quality,
    _assert_unsupported_evidence_result,
)


class TestContradictionHelpers:
    """The contradiction check the MCP tool wraps."""

    def test_check_contradiction_returns_structured_assessment(self, monkeypatch):
        import mcp_server

        monkeypatch.setattr(
            "llm_wiki.mcp_server_vault.assess_contradiction_text",
            lambda claim: {
                "assessments": [],
                "evidence": [],
                "validity": {"status": "unverified"},
                "recommendations": ["quarantine"],
            },
        )
        result = mcp_server.check_contradiction("plain string claim")
        assert set(result) == {
            "assessments",
            "evidence",
            "validity",
            "recommendations",
        }

    def test_real_string_assessment_retrieves_claim_index_and_reports_unsupported_evidence(
        self, tmp_path, monkeypatch
    ):
        import contradiction_pipeline
        import mcp_server

        vault = tmp_path / "vault"
        state = tmp_path / "state"
        (vault / "knowledge/notes").mkdir(parents=True)
        state.mkdir()
        monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
        monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
        monkeypatch.setattr(
            contradiction_pipeline,
            "default_secondary_search",
            lambda root, query, limit: [
                {"path": "knowledge/notes/context.md", "title": "Context", "snippet": "project state blue"}
            ],
        )

        result = mcp_server.assess_contradiction_text("project has-state red")

        _assert_unsupported_evidence_result(result)
        _assert_unsupported_evidence_quality(
            mcp_server.quality_for("check_contradiction", result)
        )
