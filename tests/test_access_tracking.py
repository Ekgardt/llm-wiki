"""Tests for access_tracking.py — retrieval analytics and decay scoring."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


class TestRecordAccess:
    """Test record_access accumulates in memory (no disk I/O)."""

    def test_record_accumulates_in_batch(self, tmp_path, monkeypatch):
        """record_access should accumulate in memory batch without disk I/O."""
        import access_tracking

        monkeypatch.setattr(access_tracking, "ACCESS_LOG_FILE", tmp_path / "access_log.jsonl")
        monkeypatch.setattr(access_tracking, "KNOWLEDGE_DIR", tmp_path / "notes")
        access_tracking._batch.clear()

        access_tracking.record_access("test-page", source="search", query="auth")
        access_tracking.record_access("test-page", source="search", query="auth")

        assert access_tracking._batch.get("test-page", 0) == 2

    def test_record_batch_accumulates(self, tmp_path, monkeypatch):
        """Multiple accesses accumulate in the batch."""
        import access_tracking

        monkeypatch.setattr(access_tracking, "ACCESS_LOG_FILE", tmp_path / "access_log.jsonl")
        monkeypatch.setattr(access_tracking, "KNOWLEDGE_DIR", tmp_path / "notes")
        access_tracking._batch.clear()

        for _ in range(3):
            access_tracking.record_access("batch-page", source="search")

        assert access_tracking._batch.get("batch-page", 0) == 3

    def test_record_without_query_or_rank(self, tmp_path, monkeypatch):
        """record_access works without optional query/rank params."""
        import access_tracking

        monkeypatch.setattr(access_tracking, "ACCESS_LOG_FILE", tmp_path / "access_log.jsonl")
        monkeypatch.setattr(access_tracking, "KNOWLEDGE_DIR", tmp_path / "notes")
        access_tracking._batch.clear()

        access_tracking.record_access("simple", source="direct")
        assert access_tracking._batch.get("simple", 0) == 1


class TestGetAccessStats:
    """Test get_access_stats reads the JSONL log correctly."""

    def test_stats_for_no_access(self, tmp_path, monkeypatch):
        import access_tracking

        monkeypatch.setattr(access_tracking, "ACCESS_LOG_FILE", tmp_path / "nonexistent.jsonl")
        stats = access_tracking.get_access_stats("never-accessed")
        assert stats["total_count"] == 0
        assert stats["last_accessed"] is None

    def test_stats_counts_correctly(self, tmp_path, monkeypatch):
        import access_tracking

        log_file = tmp_path / "access_log.jsonl"
        log_file.write_text(
            json.dumps({"slug": "page-a", "source": "search", "timestamp": "2026-01-01T10:00:00"}) + "\n"
            + json.dumps({"slug": "page-a", "source": "direct", "timestamp": "2026-01-02T10:00:00"}) + "\n"
            + json.dumps({"slug": "page-b", "source": "search", "timestamp": "2026-01-03T10:00:00"}) + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(access_tracking, "ACCESS_LOG_FILE", log_file)

        stats = access_tracking.get_access_stats("page-a")
        assert stats["total_count"] == 2
        assert stats["last_accessed"] == "2026-01-02T10:00:00"
        assert stats["sources"]["search"] == 1
        assert stats["sources"]["direct"] == 1


class TestDecayScore:
    """Test Ebbinghaus-inspired decay scoring."""

    def test_decision_never_decays(self, tmp_path, monkeypatch):
        """Decisions have infinite half-life — always score high."""
        import access_tracking

        monkeypatch.setattr(access_tracking, "ACCESS_LOG_FILE", tmp_path / "no.jsonl")
        score = access_tracking.decay_score("decision-page", page_type="decision", confidence="high")
        assert score >= 0.9

    def test_concept_decays_slowly(self, tmp_path, monkeypatch):
        """Concepts decay over 365 days."""
        import access_tracking

        monkeypatch.setattr(access_tracking, "ACCESS_LOG_FILE", tmp_path / "no.jsonl")
        score = access_tracking.decay_score("concept-page", page_type="concept", confidence="medium")
        # With no access, still high (delta_t = 0)
        assert score > 0.5

    def test_debugging_decays_fast(self, tmp_path, monkeypatch):
        """Debugging pages have 30-day half-life."""
        import access_tracking

        # Simulate 60 days since last access
        log_file = tmp_path / "old.jsonl"
        log_file.write_text(
            json.dumps({"slug": "bug", "source": "search", "timestamp": "2026-05-01T00:00:00"}) + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(access_tracking, "ACCESS_LOG_FILE", log_file)
        score = access_tracking.decay_score("bug", page_type="debugging", confidence="low")
        # Should have decayed significantly
        assert score < 0.5

    def test_access_reinforces_score(self, tmp_path, monkeypatch):
        """Frequent access boosts the decay score."""
        import access_tracking

        log_file = tmp_path / "frequent.jsonl"
        lines = []
        for i in range(10):
            lines.append(json.dumps({"slug": "hot", "source": "search", "timestamp": f"2026-07-{i+1:02d}T00:00:00"}))
        log_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        monkeypatch.setattr(access_tracking, "ACCESS_LOG_FILE", log_file)

        score_hot = access_tracking.decay_score("hot", page_type="debugging", confidence="low")

        monkeypatch.setattr(access_tracking, "ACCESS_LOG_FILE", tmp_path / "no.jsonl")
        score_cold = access_tracking.decay_score("cold", page_type="debugging", confidence="low")

        assert score_hot > score_cold

    def test_score_between_0_and_1(self, tmp_path, monkeypatch):
        import access_tracking

        monkeypatch.setattr(access_tracking, "ACCESS_LOG_FILE", tmp_path / "no.jsonl")
        for ptype in ["debugging", "pattern", "concept", "decision", "qa"]:
            score = access_tracking.decay_score("x", page_type=ptype, confidence="medium")
            assert 0.0 <= score <= 1.0, f"{ptype}: {score}"


class TestFlushFrontmatter:
    """Test flushing access counts to page frontmatter."""

    def test_flush_adds_frontmatter_to_bare_page(self, tmp_path, monkeypatch):
        import access_tracking

        vault = tmp_path / "vault"
        notes_dir = vault / "knowledge" / "notes"
        notes_dir.mkdir(parents=True)
        monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
        monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "state"))
        page = notes_dir / "bare.md"
        page.write_text("# Bare Page\n\nBody.\n", encoding="utf-8")

        monkeypatch.setattr(access_tracking, "KNOWLEDGE_DIR", notes_dir)
        monkeypatch.setattr(access_tracking, "ACCESS_LOG_FILE", tmp_path / "log.jsonl")
        access_tracking._batch.clear()
        access_tracking._batch["bare"] = 3

        n = access_tracking.flush_access_to_frontmatter("bare")
        assert n == 1

        content = page.read_text(encoding="utf-8")
        assert "access_count: 3" in content
        assert "last_accessed:" in content

    def test_flush_updates_existing_frontmatter(self, tmp_path, monkeypatch):
        import access_tracking

        vault = tmp_path / "vault"
        notes_dir = vault / "knowledge" / "notes"
        notes_dir.mkdir(parents=True)
        monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
        monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "state"))
        page = notes_dir / "fm.md"
        page.write_text(
            "---\ntype: concept\naccess_count: 5\nlast_accessed: 2026-01-01T00:00:00\n---\n\n# FM\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(access_tracking, "KNOWLEDGE_DIR", notes_dir)
        monkeypatch.setattr(access_tracking, "ACCESS_LOG_FILE", tmp_path / "log.jsonl")
        access_tracking._batch.clear()
        access_tracking._batch["fm"] = 2

        n = access_tracking.flush_access_to_frontmatter("fm")
        assert n == 1

        content = page.read_text(encoding="utf-8")
        assert "access_count: 7" in content  # 5 + 2
        assert "2026-01-01T00:00:00" not in content  # old timestamp replaced

    def test_flush_handles_missing_page(self, tmp_path, monkeypatch):
        """Flushing a non-existent page should not crash."""
        import access_tracking

        monkeypatch.setattr(access_tracking, "KNOWLEDGE_DIR", tmp_path / "notes")
        access_tracking._batch.clear()
        access_tracking._batch["nonexistent"] = 5

        n = access_tracking.flush_access_to_frontmatter("nonexistent")
        assert n == 0
