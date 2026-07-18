"""Tests for access_tracking.py — retrieval analytics and decay scoring."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


class TestRecordAccess:
    """Test record_access immediately adapts to durable telemetry."""

    def test_record_search_access_writes_impression_not_batch(self, tmp_path, monkeypatch):
        import access_tracking
        import retrieval_telemetry

        database = tmp_path / "cache/evidence-graph/telemetry.sqlite3"
        monkeypatch.setattr(retrieval_telemetry, "TELEMETRY_DB", database)
        access_tracking.record_access("test-page", source="search", query="auth", rank=2)

        rows = retrieval_telemetry.read_events(limit=10, db_path=database)
        assert [(row.event_kind, row.candidate_id, row.rank) for row in rows] == [
            ("impression", "test-page", 2)
        ]

    @pytest.mark.parametrize(
        ("source", "kind"),
        [("direct", "page_read"), ("session-start", "context_injected")],
    )
    def test_record_maps_legacy_sources(self, tmp_path, monkeypatch, source, kind):
        import access_tracking
        import retrieval_telemetry

        database = tmp_path / "cache/evidence-graph/telemetry.sqlite3"
        monkeypatch.setattr(retrieval_telemetry, "TELEMETRY_DB", database)
        access_tracking.record_access("page", source=source)

        assert retrieval_telemetry.read_events(limit=1, db_path=database)[0].event_kind == kind

    def test_record_failure_is_best_effort(self, tmp_path, monkeypatch):
        import access_tracking
        import retrieval_telemetry

        monkeypatch.setattr(retrieval_telemetry, "TELEMETRY_DB", tmp_path / "bad")
        (tmp_path / "bad").write_text("not sqlite", encoding="utf-8")
        access_tracking.record_access("simple", source="direct")


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

    def test_stats_merge_bounded_telemetry_and_legacy_history(self, tmp_path, monkeypatch):
        import access_tracking
        import retrieval_telemetry

        database = tmp_path / "cache/evidence-graph/telemetry.sqlite3"
        monkeypatch.setattr(retrieval_telemetry, "TELEMETRY_DB", database)
        event = retrieval_telemetry.make_event(
            event_kind="page_read", query=None, retrieval_mode="direct",
            candidate_id="page", rank=None, generation="legacy", source_tool="new",
        )
        retrieval_telemetry.record_event(event, db_path=database)
        legacy = tmp_path / "access_log.jsonl"
        legacy.write_text(
            json.dumps({"slug": "page", "source": "search", "timestamp": "2026-01-01T00:00:00"}) + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(access_tracking, "ACCESS_LOG_FILE", legacy)

        stats = access_tracking.get_access_stats("page")
        assert stats["total_count"] == 2
        assert stats["sources"] == {"new": 1, "search": 1}

    def test_legacy_stats_are_bounded_and_reject_symlink(self, tmp_path, monkeypatch):
        import access_tracking

        legacy = tmp_path / "access_log.jsonl"
        legacy.write_bytes(b"x" * 33)
        monkeypatch.setattr(access_tracking, "ACCESS_LOG_FILE", legacy)
        monkeypatch.setattr(access_tracking, "MAX_LEGACY_ACCESS_LOG_BYTES", 32)
        assert access_tracking.get_access_stats("page")["total_count"] == 0

        target = tmp_path / "target.jsonl"
        target.write_text("", encoding="utf-8")
        link = tmp_path / "link.jsonl"
        try:
            link.symlink_to(target)
        except OSError:
            pytest.skip("symlink creation unavailable")
        monkeypatch.setattr(access_tracking, "ACCESS_LOG_FILE", link)
        assert access_tracking.get_access_stats("page")["total_count"] == 0


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

    def test_timezone_aware_telemetry_timestamp_decays(self, monkeypatch):
        import access_tracking

        monkeypatch.setattr(
            access_tracking,
            "get_access_stats",
            lambda slug: {
                "total_count": 0,
                "last_accessed": "2020-01-01T00:00:00.000000Z",
                "sources": {},
            },
        )

        assert access_tracking.decay_score(
            "old", page_type="debugging", confidence="low"
        ) < 0.1


class TestFlushFrontmatter:
    """Test explicit idempotent export from durable telemetry."""

    @staticmethod
    def _events(database, slug, count):
        import retrieval_telemetry

        retrieval_telemetry.record_events(
            [
                retrieval_telemetry.make_event(
                    event_kind="page_read", query=None, retrieval_mode="direct",
                    candidate_id=slug, rank=None, generation="legacy", source_tool="test",
                )
                for _ in range(count)
            ],
            db_path=database,
        )

    def test_flush_adds_frontmatter_to_bare_page(self, tmp_path, monkeypatch):
        import access_tracking
        import retrieval_telemetry

        vault = tmp_path / "vault"
        notes_dir = vault / "knowledge" / "notes"
        notes_dir.mkdir(parents=True)
        monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
        monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "state"))
        page = notes_dir / "bare.md"
        page.write_text("# Bare Page\n\nBody.\n", encoding="utf-8")

        monkeypatch.setattr(access_tracking, "KNOWLEDGE_DIR", notes_dir)
        database = tmp_path / "state/cache/evidence-graph/telemetry.sqlite3"
        monkeypatch.setattr(retrieval_telemetry, "TELEMETRY_DB", database)
        self._events(database, "bare", 3)

        n = access_tracking.flush_access_to_frontmatter("bare")
        assert n == 1

        content = page.read_text(encoding="utf-8")
        assert "access_count: 3" in content
        assert "last_accessed:" in content
        assert "access_telemetry_sequence: 3" in content

    def test_flush_updates_existing_frontmatter(self, tmp_path, monkeypatch):
        import access_tracking
        import retrieval_telemetry

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
        database = tmp_path / "state/cache/evidence-graph/telemetry.sqlite3"
        monkeypatch.setattr(retrieval_telemetry, "TELEMETRY_DB", database)
        self._events(database, "fm", 2)

        n = access_tracking.flush_access_to_frontmatter("fm")
        assert n == 1

        content = page.read_text(encoding="utf-8")
        assert "access_count: 7" in content  # 5 + 2
        assert "2026-01-01T00:00:00" not in content  # old timestamp replaced
        assert "access_telemetry_sequence: 2" in content

        assert access_tracking.flush_access_to_frontmatter("fm") == 0
        assert "access_count: 7" in page.read_text(encoding="utf-8")

    def test_flush_accepts_quoted_keys_values_and_inline_comments(
        self, tmp_path, monkeypatch
    ):
        import access_tracking
        import retrieval_telemetry

        vault = tmp_path / "vault"
        notes = vault / "knowledge/notes"
        notes.mkdir(parents=True)
        page = notes / "quoted.md"
        page.write_text(
            "---\ntype: concept\n\"access_count\": '5' # old count\n"
            "'access_telemetry_sequence': \"0\" # old watermark\n---\n# Quoted\n",
            encoding="utf-8",
        )
        database = tmp_path / "state/cache/evidence-graph/telemetry.sqlite3"
        monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
        monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setattr(access_tracking, "KNOWLEDGE_DIR", notes)
        monkeypatch.setattr(retrieval_telemetry, "TELEMETRY_DB", database)
        self._events(database, "quoted", 2)

        assert access_tracking.flush_access_to_frontmatter("quoted") == 1
        content = page.read_text(encoding="utf-8")
        assert content.count("access_count:") == 1
        assert content.count("access_telemetry_sequence:") == 1
        assert "access_count: 7" in content
        assert "access_telemetry_sequence: 2" in content
        assert access_tracking.flush_access_to_frontmatter("quoted") == 0

    @pytest.mark.parametrize(
        "fields",
        [
            "access_count: 1\n'access_count': '2'",
            '"access_telemetry_sequence": "0"\naccess_telemetry_sequence: 0',
            "access_count: -1",
            "access_count: 'invalid' # malformed",
            'access_telemetry_sequence: "-1"',
            "'access_telemetry_sequence': nope",
        ],
    )
    def test_flush_rejects_duplicate_negative_or_malformed_access_fields(
        self, tmp_path, monkeypatch, fields
    ):
        import access_tracking
        import retrieval_telemetry

        vault = tmp_path / "vault"
        notes = vault / "knowledge/notes"
        notes.mkdir(parents=True)
        page = notes / "invalid.md"
        page.write_text(
            f"---\ntype: concept\n{fields}\n---\n# Invalid\n", encoding="utf-8"
        )
        before = page.read_bytes()
        database = tmp_path / "state/cache/evidence-graph/telemetry.sqlite3"
        monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
        monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setattr(access_tracking, "KNOWLEDGE_DIR", notes)
        monkeypatch.setattr(retrieval_telemetry, "TELEMETRY_DB", database)
        self._events(database, "invalid", 1)

        assert access_tracking.flush_access_to_frontmatter("invalid") == 0
        assert page.read_bytes() == before

    def test_flush_handles_missing_page(self, tmp_path, monkeypatch):
        """Flushing a non-existent page should not crash."""
        import access_tracking
        import retrieval_telemetry

        monkeypatch.setattr(access_tracking, "KNOWLEDGE_DIR", tmp_path / "notes")
        database = tmp_path / "cache/evidence-graph/telemetry.sqlite3"
        monkeypatch.setattr(retrieval_telemetry, "TELEMETRY_DB", database)
        self._events(database, "nonexistent", 5)

        n = access_tracking.flush_access_to_frontmatter("nonexistent")
        assert n == 0

    def test_flush_conflict_preserves_user_edit_and_retry_exports_events(
        self, tmp_path, monkeypatch
    ):
        import access_tracking
        import markdown_transaction
        import retrieval_telemetry

        vault = tmp_path / "vault"
        notes = vault / "knowledge" / "notes"
        notes.mkdir(parents=True)
        page = notes / "page.md"
        page.write_text("---\ntype: concept\n---\n# Original\n", encoding="utf-8")
        monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
        monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setattr(access_tracking, "KNOWLEDGE_DIR", notes)
        database = tmp_path / "state/cache/evidence-graph/telemetry.sqlite3"
        monkeypatch.setattr(retrieval_telemetry, "TELEMETRY_DB", database)
        self._events(database, "page", 3)
        user_bytes = b"---\ntype: concept\n---\n# User edit\n"
        original_mutate = markdown_transaction.mutate_knowledge

        def race(operation_id, changes, **kwargs):
            page.write_bytes(user_bytes)
            return original_mutate(operation_id, changes, **kwargs)

        monkeypatch.setattr(access_tracking, "mutate_knowledge", race)

        assert access_tracking.flush_access_to_frontmatter("page") == 0
        assert page.read_bytes() == user_bytes
        monkeypatch.setattr(access_tracking, "mutate_knowledge", original_mutate)
        assert access_tracking.flush_access_to_frontmatter("page") == 1
        assert "access_count: 3" in page.read_text(encoding="utf-8")

    def test_flush_rejects_page_above_snapshot_bound(self, tmp_path, monkeypatch):
        import access_tracking
        import retrieval_telemetry

        notes = tmp_path / "knowledge" / "notes"
        notes.mkdir(parents=True)
        page = notes / "large.md"
        page.write_bytes(b"x" * 33)
        monkeypatch.setattr(access_tracking, "KNOWLEDGE_DIR", notes)
        monkeypatch.setattr(access_tracking, "MAX_ACCESS_PAGE_BYTES", 32)
        database = tmp_path / "cache/evidence-graph/telemetry.sqlite3"
        monkeypatch.setattr(retrieval_telemetry, "TELEMETRY_DB", database)
        self._events(database, "large", 1)

        assert access_tracking.flush_access_to_frontmatter("large") == 0
        assert page.read_bytes() == b"x" * 33

    def test_flush_all_is_manual_only_and_never_appends_legacy_jsonl(self, tmp_path, monkeypatch):
        import access_tracking

        legacy = tmp_path / "access_log.jsonl"
        legacy.write_text("historic\n", encoding="utf-8")
        monkeypatch.setattr(access_tracking, "ACCESS_LOG_FILE", legacy)
        monkeypatch.setattr(access_tracking, "KNOWLEDGE_DIR", tmp_path / "notes")

        assert access_tracking.flush_all() == 0
        assert legacy.read_text(encoding="utf-8") == "historic\n"

    def test_event_limit_continues_from_page_watermark(self, tmp_path, monkeypatch):
        import access_tracking
        import retrieval_telemetry

        notes = tmp_path / "vault/knowledge/notes"
        notes.mkdir(parents=True)
        page = notes / "page.md"
        page.write_text("---\ntype: concept\n---\n# Page\n", encoding="utf-8")
        database = tmp_path / "state/cache/evidence-graph/telemetry.sqlite3"
        monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path / "vault"))
        monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setattr(access_tracking, "KNOWLEDGE_DIR", notes)
        monkeypatch.setattr(access_tracking, "MAX_EVENTS_PER_PAGE_EXPORT", 2)
        monkeypatch.setattr(retrieval_telemetry, "TELEMETRY_DB", database)
        self._events(database, "page", 5)

        assert [access_tracking.flush_all() for _ in range(3)] == [1, 1, 1]
        content = page.read_text(encoding="utf-8")
        assert "access_count: 5" in content
        assert "access_telemetry_sequence: 5" in content
        assert access_tracking.flush_all() == 0

    def test_subprocess_restart_promotion_is_idempotent(self, tmp_path):
        vault = tmp_path / "vault"
        state = tmp_path / "state"
        notes = vault / "knowledge/notes"
        notes.mkdir(parents=True)
        page = notes / "page.md"
        page.write_text("---\ntype: concept\n---\n# Page\n", encoding="utf-8")
        env = dict(os.environ)
        env["LLM_WIKI_ROOT"] = str(vault)
        env["LLM_WIKI_STATE_ROOT"] = str(state)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent / "scripts")
        record = (
            "from retrieval_telemetry import *; "
            "record_events([make_event(event_kind='page_read', query=None, retrieval_mode='direct', "
            "candidate_id='page', rank=None, generation='legacy', source_tool='restart') for _ in range(3)])"
        )
        subprocess.run([sys.executable, "-c", record], env=env, check=True)
        command = [sys.executable, str(Path(__file__).resolve().parent.parent / "scripts/access_tracking.py"), "--flush"]
        subprocess.run(command, env=env, check=True)
        subprocess.run(command, env=env, check=True)

        content = page.read_text(encoding="utf-8")
        assert "access_count: 3" in content
        assert content.count("access_telemetry_sequence:") == 1

    def test_flush_all_skips_non_page_candidates_without_blocking_pages(self, tmp_path, monkeypatch):
        import access_tracking
        import retrieval_telemetry

        notes = tmp_path / "vault/knowledge/notes"
        notes.mkdir(parents=True)
        (notes / "page.md").write_text("---\ntype: concept\n---\n# Page\n", encoding="utf-8")
        database = tmp_path / "state/cache/evidence-graph/telemetry.sqlite3"
        monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path / "vault"))
        monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setattr(access_tracking, "KNOWLEDGE_DIR", notes)
        monkeypatch.setattr(retrieval_telemetry, "TELEMETRY_DB", database)
        self._events(database, "a" * 64, 2)
        self._events(database, "page", 1)

        assert access_tracking.flush_all() == 1
        assert "access_count: 1" in (notes / "page.md").read_text(encoding="utf-8")

    def test_cyclic_cursor_reaches_more_candidates_than_one_scan(self, tmp_path, monkeypatch):
        import access_tracking
        import retrieval_telemetry

        vault = tmp_path / "vault"
        state = tmp_path / "state"
        notes = vault / "knowledge/notes"
        notes.mkdir(parents=True)
        database = state / "cache/evidence-graph/telemetry.sqlite3"
        monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
        monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
        monkeypatch.setattr(access_tracking, "KNOWLEDGE_DIR", notes)
        monkeypatch.setattr(access_tracking, "MAX_PAGES_PER_EXPORT", 1)
        monkeypatch.setattr(access_tracking, "MAX_CANDIDATES_SCANNED_PER_EXPORT", 2)
        monkeypatch.setattr(retrieval_telemetry, "TELEMETRY_DB", database)
        for slug in ("alpha", "bravo", "charlie", "delta", "echo"):
            (notes / f"{slug}.md").write_text(
                f"---\ntype: concept\n---\n# {slug}\n", encoding="utf-8"
            )
            self._events(database, slug, 1)

        assert [access_tracking.flush_all() for _ in range(5)] == [1, 1, 1, 1, 1]
        for slug in ("alpha", "bravo", "charlie", "delta", "echo"):
            assert "access_count: 1" in (notes / f"{slug}.md").read_text(encoding="utf-8")
        assert retrieval_telemetry.get_export_cursor(db_path=database) == "echo"

    def test_cursor_wrap_reaches_new_candidate_behind_cursor(self, tmp_path, monkeypatch):
        import access_tracking
        import retrieval_telemetry

        vault = tmp_path / "vault"
        state = tmp_path / "state"
        notes = vault / "knowledge/notes"
        notes.mkdir(parents=True)
        database = state / "cache/evidence-graph/telemetry.sqlite3"
        monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
        monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
        monkeypatch.setattr(access_tracking, "KNOWLEDGE_DIR", notes)
        monkeypatch.setattr(access_tracking, "MAX_PAGES_PER_EXPORT", 2)
        monkeypatch.setattr(retrieval_telemetry, "TELEMETRY_DB", database)
        for slug in ("alpha", "zulu"):
            (notes / f"{slug}.md").write_text(
                f"---\ntype: concept\n---\n# {slug}\n", encoding="utf-8"
            )
            self._events(database, slug, 1)
        retrieval_telemetry.set_export_cursor("middle", db_path=database)

        assert access_tracking.flush_all() == 2
        assert "access_count: 1" in (notes / "zulu.md").read_text(encoding="utf-8")
        assert "access_count: 1" in (notes / "alpha.md").read_text(encoding="utf-8")
        assert retrieval_telemetry.get_export_cursor(db_path=database) == "alpha"

    def test_cursor_update_failure_retries_without_duplicate_count(self, tmp_path, monkeypatch):
        import access_tracking
        import retrieval_telemetry

        vault = tmp_path / "vault"
        state = tmp_path / "state"
        notes = vault / "knowledge/notes"
        notes.mkdir(parents=True)
        page = notes / "alpha.md"
        page.write_text("---\ntype: concept\n---\n# Alpha\n", encoding="utf-8")
        database = state / "cache/evidence-graph/telemetry.sqlite3"
        monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
        monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
        monkeypatch.setattr(access_tracking, "KNOWLEDGE_DIR", notes)
        monkeypatch.setattr(retrieval_telemetry, "TELEMETRY_DB", database)
        self._events(database, "alpha", 2)
        real_set = retrieval_telemetry.set_export_cursor
        attempts = 0

        def fail_once(value, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("simulated cursor write failure")
            return real_set(value, **kwargs)

        monkeypatch.setattr(retrieval_telemetry, "set_export_cursor", fail_once)

        assert access_tracking.flush_all() == 1
        assert access_tracking.flush_all() == 0
        content = page.read_text(encoding="utf-8")
        assert "access_count: 2" in content
        assert retrieval_telemetry.get_export_cursor(db_path=database) == "alpha"

    def test_direct_export_does_not_change_global_cursor(self, tmp_path, monkeypatch):
        import access_tracking
        import retrieval_telemetry

        vault = tmp_path / "vault"
        state = tmp_path / "state"
        notes = vault / "knowledge/notes"
        notes.mkdir(parents=True)
        (notes / "page.md").write_text("---\ntype: concept\n---\n# Page\n", encoding="utf-8")
        database = state / "cache/evidence-graph/telemetry.sqlite3"
        monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
        monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
        monkeypatch.setattr(access_tracking, "KNOWLEDGE_DIR", notes)
        monkeypatch.setattr(retrieval_telemetry, "TELEMETRY_DB", database)
        self._events(database, "page", 1)
        retrieval_telemetry.set_export_cursor("middle", db_path=database)

        assert access_tracking.flush_access_to_frontmatter("page") == 1
        assert retrieval_telemetry.get_export_cursor(db_path=database) == "middle"

    def test_corrupt_cursor_fails_closed_without_page_mutation(self, tmp_path, monkeypatch):
        import access_tracking
        import retrieval_telemetry

        vault = tmp_path / "vault"
        state = tmp_path / "state"
        notes = vault / "knowledge/notes"
        notes.mkdir(parents=True)
        page = notes / "alpha.md"
        page.write_text("---\ntype: concept\n---\n# Alpha\n", encoding="utf-8")
        before = page.read_bytes()
        database = state / "cache/evidence-graph/telemetry.sqlite3"
        monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
        monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
        monkeypatch.setattr(access_tracking, "KNOWLEDGE_DIR", notes)
        monkeypatch.setattr(retrieval_telemetry, "TELEMETRY_DB", database)
        self._events(database, "alpha", 1)
        retrieval_telemetry.set_export_cursor("valid", db_path=database)
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE telemetry_state SET value = ? WHERE key = ?",
                ("bad\nvalue", "access_export_cursor"),
            )
            connection.commit()

        assert access_tracking.flush_all() == 0
        assert page.read_bytes() == before
