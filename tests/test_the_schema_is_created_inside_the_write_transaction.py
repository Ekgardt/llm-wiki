"""Created outside it, `CREATE … IF NOT EXISTS` promotes a read lock to a write lock,
the one wait SQLite refuses: five of eight writers on Windows got `database is
locked` at once although the connection had a busy timeout (CI run 35941975284,
2026-09-24). See docs/research/2026-09-24-two-post-merge-failures-on-main.md.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import retrieval_telemetry  # noqa: E402
import trace_ingest  # noqa: E402


def _first_index(statements: list[str], prefix: str) -> int:
    return next(i for i, s in enumerate(statements) if s.lstrip().startswith(prefix))


def test_telemetry_creates_its_schema_after_begin_immediate(tmp_path, monkeypatch):
    statements: list[str] = []
    opened = retrieval_telemetry._open_write_database

    def traced(path, *, busy_ms):
        database = opened(path, busy_ms=busy_ms)
        database.set_trace_callback(statements.append)
        return database

    monkeypatch.setattr(retrieval_telemetry, "_open_write_database", traced)
    event = retrieval_telemetry.make_event(
        event_kind="page_read",
        query=None,
        retrieval_mode="direct",
        candidate_id="page-1",
        rank=None,
        generation="legacy",
        source_tool="test",
    )

    retrieval_telemetry.record_events(
        [event], db_path=tmp_path / "state/cache/evidence-graph/telemetry.sqlite3"
    )

    assert _first_index(statements, "BEGIN IMMEDIATE") < _first_index(statements, "CREATE")


def test_the_trace_store_creates_its_schema_after_begin_immediate(tmp_path, monkeypatch):
    statements: list[str] = []
    configured = trace_ingest._configure

    def traced(database):
        database.set_trace_callback(statements.append)
        return configured(database)

    monkeypatch.setattr(trace_ingest, "_configure", traced)

    database = trace_ingest.open_store(tmp_path)
    open_transaction = database.in_transaction
    database.close()

    assert _first_index(statements, "BEGIN IMMEDIATE") < _first_index(statements, "CREATE")
    assert open_transaction is False
