from __future__ import annotations

import sqlite3
import time

import mcp_server
import search_memory


def test_real_search_accepts_a_requested_count_above_the_former_ceiling(tmp_path, monkeypatch):
    notes = tmp_path / "knowledge/notes"
    notes.mkdir(parents=True)
    (notes / "needle.md").write_text("---\ntype: concept\n---\n# Needle\nneedle evidence\n", encoding="utf-8")
    monkeypatch.setattr(search_memory, "ROOT", tmp_path)
    monkeypatch.setattr(search_memory, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(search_memory, "KNOWLEDGE_DIR", notes)
    monkeypatch.setattr(search_memory, "WIKI_DIR", notes)
    rows = search_memory.search("needle", limit=1001, semantic=False, rerank=False,
                                emit_telemetry=False, deadline_monotonic=time.monotonic() + 30)
    assert [row["path"] for row in rows] == ["knowledge/notes/needle.md"]
    assert rows[0]["fallback_reason"] == "no_active_generation"


def test_large_requested_count_stays_a_valid_sqlite_limit():
    connection = sqlite3.connect(":memory:")
    connection.executescript(search_memory._GENERATION_FTS_DDL)
    connection.execute("INSERT INTO chunks(chunk_id,chunk_order,source_path,title,content) VALUES(?,?,?,?,?)",
                       ("one", 0, "knowledge/notes/one.md", "needle", "needle"))
    rows = search_memory._generation_cohort_rows(connection, "needle", "", [], 2**63, sessions=False)
    assert len(rows) == 1
    assert rows[0][0] == "one"
    connection.close()


def test_decision_pool_does_not_underfetch_the_requested_page_count(monkeypatch):
    asked = []

    def observe_pool(_query, count, **_options):
        asked.append(count)
        return []

    monkeypatch.setattr(mcp_server, "_search_vault", observe_pool)
    monkeypatch.setattr(mcp_server, "_record_decision_impressions", lambda *_args: None)
    assert mcp_server._get_decisions("needle", limit=1001) == []
    assert asked[0] >= 1001
