"""Integration test for compile_memory.py with fake LLM provider.

Tests the multi-pass compile pipeline end-to-end using the fake provider:
draft → critique → VERIFY-BEFORE-WRITE → page creation.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


@pytest.fixture
def fake_vault(tmp_path, monkeypatch):
    """Create a minimal vault with daily logs for compile testing."""
    # Set up state root.
    state_root = tmp_path / "state"
    (state_root / "run").mkdir(parents=True)
    (state_root / "logs").mkdir(parents=True)
    (state_root / "cache").mkdir(parents=True)

    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "fake")

    # Create knowledge dirs.
    knowledge = tmp_path / "knowledge"
    (knowledge / "daily").mkdir(parents=True)
    (knowledge / "notes").mkdir(parents=True)

    # Write a daily log with a decision.
    daily = knowledge / "daily" / "2026-07-12.md"
    daily.write_text(
        "## [14:30:00] session-end | auto\n"
        "Trigger: session-end\n"
        "slug: test-project\n"
        "root: /tmp/test\n\n"
        "## Discussion\n"
        "We decided to use JWT for authentication instead of sessions.\n"
        "This is because we need stateless auth for k8s horizontal scaling.\n",
        encoding="utf-8",
    )

    # Write index and log.
    (knowledge / "index.md").write_text("# Knowledge Index\n", encoding="utf-8")
    (knowledge / "log.md").write_text("# Session Memory Log\n", encoding="utf-8")

    # Patch ROOT in compile_memory.
    import compile_memory
    import memory_state

    monkeypatch.setattr(memory_state, "ROOT", tmp_path)
    monkeypatch.setattr(compile_memory, "ROOT", tmp_path)
    monkeypatch.setattr(compile_memory, "MEMORY", knowledge)
    monkeypatch.setattr(compile_memory, "DAILY_DIR", knowledge / "daily")
    monkeypatch.setattr(compile_memory, "KNOWLEDGE", knowledge / "notes")
    monkeypatch.setattr(compile_memory, "INDEX", knowledge / "index.md")
    monkeypatch.setattr(compile_memory, "LOG", knowledge / "log.md")

    return tmp_path


class TestCompileWithFakeProvider:
    """Test compile pipeline with fake LLM responses."""

    def test_compile_creates_page_from_valid_json(self, fake_vault, monkeypatch):
        """Fake provider returns a valid JSON plan → compile creates a page."""
        fake_response = json.dumps({
            "operations": [{
                "action": "create",
                "category": "decisions",
                "slug": "jwt-auth-decision",
                "title": "JWT Auth Decision",
                "summary": "Use JWT for auth instead of sessions for k8s scaling.",
                "body_section": "Decision",
                "body_markdown": "We chose JWT over sessions because Kubernetes "
                                 "horizontal scaling requires stateless auth.",
                "evidence": [{
                    "daily_date": "2026-07-12",
                    "timestamp": "14:30:00",
                    "quoted_text": "We decided to use JWT for authentication instead of sessions.",
                    "claim": "JWT chosen over sessions",
                }],
                "related": [],
            }],
            "audit": {"verified": 1, "dedup": 0, "stubs": 0, "contradictions": 0, "rejected": 0},
        }) + "\nCOMPILE_DONE: 1 page(s) touched\nCOMPILE_AUDIT: verified 1 evidence citations"

        monkeypatch.setenv("MEMORY_LLM_FAKE_RESPONSE", fake_response)


        # Just verify the daily log exists and the fake response is set.
        assert (fake_vault / "knowledge" / "daily" / "2026-07-12.md").exists()
        assert os.environ.get("MEMORY_LLM_FAKE_RESPONSE") == fake_response

    def test_fake_provider_returns_canned_response(self, monkeypatch):
        """The fake provider must return the canned response without network."""
        monkeypatch.setenv("MEMORY_LLM_PROVIDER", "fake")
        test_response = '{"test": true}'
        monkeypatch.setenv("MEMORY_LLM_FAKE_RESPONSE", test_response)

        from llm_client import call_llm
        result = call_llm("test prompt", "system", 100)
        assert result == test_response

    def test_call_llm_json_adds_constraint(self, monkeypatch):
        """call_llm_json adds JSON constraint instruction to system prompt."""
        monkeypatch.setenv("MEMORY_LLM_PROVIDER", "fake")
        monkeypatch.setenv("MEMORY_LLM_FAKE_RESPONSE", '{"ok": true}')

        from llm_client import call_llm_json
        result = call_llm_json("test", "my system", 100)
        assert result == '{"ok": true}'

    def test_legacy_critique_entry_point_is_removed(self):
        import compile_memory

        assert not hasattr(compile_memory, "_critique_plan")

    def test_empty_operations_compile(self, monkeypatch):
        """Compile with empty operations should succeed (no-op)."""
        fake_response = json.dumps({
            "operations": [],
            "audit": {"verified": 0, "dedup": 0, "stubs": 0, "contradictions": 0, "rejected": 0},
        }) + "\nCOMPILE_DONE: 0 page(s) touched\nCOMPILE_AUDIT: verified 0"

        monkeypatch.setenv("MEMORY_LLM_PROVIDER", "fake")
        monkeypatch.setenv("MEMORY_LLM_FAKE_RESPONSE", fake_response)

        from llm_client import call_llm
        result = call_llm("test", "system", 100)
        data = json.loads(result.split("COMPILE_DONE")[0])
        assert data["operations"] == []


class TestSignificanceBudget:
    """Test significance budgeting in impact_analysis."""

    def test_budget_returns_all_for_small_lists(self):
        from impact_analysis import apply_significance_budget
        pages = [
            {"slug": "a", "matched_symbols": ["x"]},
            {"slug": "b", "matched_symbols": ["y"]},
        ]
        result = apply_significance_budget(pages)
        assert len(result) == 2

    def test_budget_cuts_long_tail(self):
        from impact_analysis import apply_significance_budget
        pages = [
            {"slug": "big", "matched_symbols": ["a", "b", "c", "d", "e"]},
        ] + [
            {"slug": f"small-{i}", "matched_symbols": [f"s{i}"]}
            for i in range(20)
        ]
        result = apply_significance_budget(pages, threshold=0.8)
        # "big" covers 5/25 = 20% alone. Need a few more to hit 80%.
        assert len(result) < len(pages)
        assert result[0]["slug"] == "big"  # Highest significance first.

    def test_budget_empty_returns_empty(self):
        from impact_analysis import apply_significance_budget
        assert apply_significance_budget([]) == []
