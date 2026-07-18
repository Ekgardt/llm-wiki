"""Durable, private retrieval telemetry contract tests."""
from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _event(**overrides):
    import retrieval_telemetry

    values = {
        "event_kind": "impression",
        "query": "private query",
        "retrieval_mode": "bm25",
        "candidate_id": "page",
        "rank": 1,
        "generation": "legacy",
        "source_tool": "test",
    }
    values.update(overrides)
    return retrieval_telemetry.make_event(**values)


def test_event_contract_is_immutable_and_hashes_query_without_retaining_it():
    secret = "distinctive-query-secret-7f8d"
    event = _event(query=secret)

    assert event.query_sha256 == hashlib.sha256(secret.encode()).hexdigest()
    assert secret not in repr(event)
    assert event.schema_version == 1
    with pytest.raises((AttributeError, TypeError)):
        event.rank = 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event_kind", "unknown"),
        ("query_sha256", "ABC"),
        ("retrieval_mode", "x\nforged"),
        ("candidate_id", "x\x00y"),
        ("rank", True),
        ("rank", 0),
        ("generation", ""),
        ("source_tool", "x" * 129),
        ("outcome", "x" * 257),
    ],
)
def test_strict_factory_rejects_invalid_or_unbounded_values(field, value):
    values = {field: value}
    if field == "query_sha256":
        values["query"] = None
    with pytest.raises((TypeError, ValueError)):
        _event(**values)


def test_hash_query_fails_closed_on_malformed_unicode():
    import retrieval_telemetry

    with pytest.raises(ValueError):
        retrieval_telemetry.hash_query("bad\ud800query")
    assert retrieval_telemetry.best_effort_make_event(
        event_kind="impression",
        query="bad\ud800query",
        retrieval_mode="bm25",
        candidate_id="page",
        rank=1,
        generation="legacy",
        source_tool="test",
    ) is None


def test_record_batch_is_durable_private_and_uses_required_pragmas(tmp_path):
    import retrieval_telemetry

    database = tmp_path / "state/cache/evidence-graph/telemetry.sqlite3"
    query_secret = "raw-query-secret-2281"
    response_secret = "raw-response-secret-9942"
    events = [
        _event(query=query_secret, candidate_id="one"),
        _event(query=query_secret, candidate_id="two", rank=2, outcome="selected"),
    ]

    assert retrieval_telemetry.record_events(events, db_path=database) == 2
    rows = retrieval_telemetry.read_events(limit=10, db_path=database)
    assert [row.candidate_id for row in rows] == ["two", "one"]
    assert all(row.query_sha256 == retrieval_telemetry.hash_query(query_secret) for row in rows)

    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(retrieval_events)")}
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert "query" not in columns
    assert "response" not in columns
    raw = database.read_bytes()
    assert query_secret.encode() not in raw
    assert response_secret.encode() not in raw
    assert retrieval_telemetry.hash_query(query_secret).encode() in raw
    assert not database.with_name(database.name + "-wal").exists()


def test_event_written_by_process_is_visible_to_another_process(tmp_path):
    database = tmp_path / "state/cache/evidence-graph/telemetry.sqlite3"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SCRIPTS)
    writer = (
        "from pathlib import Path; from retrieval_telemetry import *; "
        f"p=Path({str(database)!r}); "
        "record_event(make_event(event_kind='page_read', query=None, "
        "retrieval_mode='direct', candidate_id='durable', rank=None, "
        "generation='legacy', source_tool='subprocess'), db_path=p)"
    )
    subprocess.run([sys.executable, "-c", writer], env=env, check=True)
    reader = (
        "from pathlib import Path; from retrieval_telemetry import read_events; "
        f"print(read_events(candidate_id='durable', limit=1, db_path=Path({str(database)!r}))[0].event_kind)"
    )
    result = subprocess.run(
        [sys.executable, "-c", reader], env=env, check=True, capture_output=True, text=True
    )
    assert result.stdout.strip() == "page_read"


def test_concurrent_process_writers_do_not_lose_events(tmp_path):
    import retrieval_telemetry

    database = tmp_path / "state/cache/evidence-graph/telemetry.sqlite3"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SCRIPTS)
    code = (
        "import sys; from pathlib import Path; from retrieval_telemetry import *; "
        f"p=Path({str(database)!r}); i=sys.argv[1]; "
        "record_event(make_event(event_kind='page_read', query=None, retrieval_mode='direct', "
        "candidate_id='page-'+i, rank=None, generation='legacy', source_tool='writer'), db_path=p)"
    )
    processes = [subprocess.Popen([sys.executable, "-c", code, str(i)], env=env) for i in range(8)]
    assert [process.wait(timeout=20) for process in processes] == [0] * 8
    assert len(retrieval_telemetry.read_events(limit=8, db_path=database)) == 8


def test_compaction_is_oldest_first_and_bounded(tmp_path):
    import retrieval_telemetry

    database = tmp_path / "state/cache/evidence-graph/telemetry.sqlite3"
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    events = []
    for age in (120, 110, 100, 10, 5, 1):
        events.append(
            _event(
                candidate_id=f"age-{age}",
                timestamp=now - timedelta(days=age),
            )
        )
    retrieval_telemetry.record_events(events, db_path=database)

    first = retrieval_telemetry.compact(
        retention_days=90, max_rows=4, max_delete=2, now=now, db_path=database
    )
    assert first == 2
    remaining = retrieval_telemetry.read_events(limit=10, db_path=database)
    assert {row.candidate_id for row in remaining} == {"age-100", "age-10", "age-5", "age-1"}
    second = retrieval_telemetry.compact(
        retention_days=90, max_rows=3, max_delete=1, now=now, db_path=database
    )
    assert second == 1
    assert len(retrieval_telemetry.read_events(limit=10, db_path=database)) == 3


def test_read_limits_and_malformed_or_unsafe_paths_fail_closed(tmp_path):
    import retrieval_telemetry

    database = tmp_path / "state/cache/evidence-graph/telemetry.sqlite3"
    retrieval_telemetry.record_event(_event(), db_path=database)
    with pytest.raises(ValueError):
        retrieval_telemetry.read_events(limit=retrieval_telemetry.MAX_READ_EVENTS + 1, db_path=database)

    malformed = tmp_path / "bad.sqlite3"
    malformed.write_bytes(b"not sqlite")
    with pytest.raises(sqlite3.DatabaseError):
        retrieval_telemetry.read_events(limit=1, db_path=malformed)
    assert retrieval_telemetry.best_effort_record_event(_event(), db_path=malformed) is False

    if hasattr(os, "symlink"):
        link = tmp_path / "telemetry-link.sqlite3"
        try:
            link.symlink_to(database)
        except OSError:
            pytest.skip("symlink creation unavailable")
        with pytest.raises((OSError, PermissionError, ValueError)):
            retrieval_telemetry.read_events(limit=1, db_path=link)


def test_recording_does_not_mutate_knowledge(tmp_path):
    import retrieval_telemetry

    page = tmp_path / "vault/knowledge/notes/page.md"
    page.parent.mkdir(parents=True)
    page.write_bytes(b"---\ntype: concept\n---\n# Page\n")
    before = page.read_bytes()
    retrieval_telemetry.record_event(
        _event(), db_path=tmp_path / "state/cache/evidence-graph/telemetry.sqlite3"
    )
    assert page.read_bytes() == before


def test_bounded_sequence_apis_expose_offsets_without_changing_event(tmp_path):
    import retrieval_telemetry

    database = tmp_path / "cache/evidence-graph/telemetry.sqlite3"
    retrieval_telemetry.record_events(
        [_event(candidate_id="one"), _event(candidate_id="two", rank=2)],
        db_path=database,
    )

    candidates = retrieval_telemetry.list_candidate_ids(limit=2, db_path=database)
    rows = retrieval_telemetry.read_events_after(
        "one", after_sequence=0, limit=10, db_path=database
    )

    assert candidates == ["one", "two"]
    assert rows[0].sequence > 0
    assert rows[0].event.candidate_id == "one"
    assert not hasattr(rows[0].event, "sequence")
    assert retrieval_telemetry.count_events_after(
        "one", after_sequence=0, limit=10, db_path=database
    ) == 1
    assert retrieval_telemetry.read_events_after(
        "one", after_sequence=rows[0].sequence, limit=10, db_path=database
    ) == []


def test_export_cursor_round_trips_and_validates_values(tmp_path):
    import retrieval_telemetry

    database = tmp_path / "cache/evidence-graph/telemetry.sqlite3"
    assert retrieval_telemetry.get_export_cursor(db_path=database) == ""
    retrieval_telemetry.set_export_cursor("middle-page", db_path=database)
    assert retrieval_telemetry.get_export_cursor(db_path=database) == "middle-page"
    retrieval_telemetry.set_export_cursor("", db_path=database)
    assert retrieval_telemetry.get_export_cursor(db_path=database) == ""

    for invalid in (None, "bad\nvalue", "x" * 513):
        with pytest.raises((TypeError, ValueError)):
            retrieval_telemetry.set_export_cursor(invalid, db_path=database)


def test_candidate_listing_pages_lexically_after_cursor_with_hard_scan_bound(
    tmp_path, monkeypatch
):
    import retrieval_telemetry

    database = tmp_path / "cache/evidence-graph/telemetry.sqlite3"
    retrieval_telemetry.record_events(
        [
            _event(candidate_id="alpha"),
            _event(candidate_id="alpha"),
            _event(candidate_id="bravo"),
            _event(candidate_id="charlie"),
        ],
        db_path=database,
    )
    monkeypatch.setattr(retrieval_telemetry, "MAX_CANDIDATE_SCAN_EVENTS", 2)

    assert retrieval_telemetry.list_candidate_ids(
        after_candidate="", limit=10, db_path=database
    ) == ["alpha"]
    assert retrieval_telemetry.list_candidate_ids(
        after_candidate="alpha", limit=10, db_path=database
    ) == ["bravo", "charlie"]
    assert retrieval_telemetry.list_candidate_ids(
        after_candidate="charlie", limit=10, db_path=database
    ) == []


def test_corrupt_export_cursor_fails_closed(tmp_path):
    import retrieval_telemetry

    database = tmp_path / "cache/evidence-graph/telemetry.sqlite3"
    retrieval_telemetry.set_export_cursor("valid", db_path=database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE telemetry_state SET value = ? WHERE key = ?",
            ("bad\nvalue", "access_export_cursor"),
        )
        connection.commit()

    with pytest.raises(ValueError, match="cursor"):
        retrieval_telemetry.get_export_cursor(db_path=database)


@pytest.mark.parametrize("value", [True, 0, -1, 100_001])
def test_record_events_validates_max_rows(value, tmp_path):
    import retrieval_telemetry

    with pytest.raises((TypeError, ValueError)):
        retrieval_telemetry.record_event(
            _event(), db_path=tmp_path / "telemetry.sqlite3", max_rows=value
        )


def test_sustained_ingestion_never_exceeds_row_ceiling(tmp_path):
    import retrieval_telemetry

    database = tmp_path / "cache/evidence-graph/telemetry.sqlite3"
    for batch in range(8):
        retrieval_telemetry.record_events(
            [_event(candidate_id=f"page-{batch}-{index}") for index in range(3)],
            db_path=database,
            max_rows=5,
        )
        assert len(retrieval_telemetry.read_events(limit=10, db_path=database)) <= 5
    rows = retrieval_telemetry.read_events(limit=10, db_path=database)
    assert len(rows) == 5
    assert {row.candidate_id for row in rows}.isdisjoint(
        {"page-0-0", "page-0-1", "page-0-2"}
    )


def test_concurrent_ingestion_respects_row_ceiling(tmp_path):
    import retrieval_telemetry

    database = tmp_path / "cache/evidence-graph/telemetry.sqlite3"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SCRIPTS)
    code = (
        "import sys; from pathlib import Path; from retrieval_telemetry import *; "
        f"p=Path({str(database)!r}); i=sys.argv[1]; "
        "record_event(make_event(event_kind='page_read', query=None, retrieval_mode='direct', "
        "candidate_id='page-'+i, rank=None, generation='legacy', source_tool='writer'), "
        "db_path=p, max_rows=5)"
    )
    processes = [subprocess.Popen([sys.executable, "-c", code, str(i)], env=env) for i in range(10)]
    assert [process.wait(timeout=30) for process in processes] == [0] * 10
    assert len(retrieval_telemetry.read_events(limit=10, db_path=database)) == 5


def test_preexisting_excess_fails_without_growing(tmp_path, monkeypatch):
    import retrieval_telemetry

    database = tmp_path / "cache/evidence-graph/telemetry.sqlite3"
    retrieval_telemetry.record_events(
        [_event(candidate_id=f"old-{index}") for index in range(10)],
        db_path=database,
        max_rows=10,
    )
    monkeypatch.setattr(retrieval_telemetry, "MAX_READ_EVENTS", 3)

    with pytest.raises(ValueError, match="bounded repair"):
        retrieval_telemetry.record_event(
            _event(candidate_id="new"), db_path=database, max_rows=2
        )
    assert retrieval_telemetry.best_effort_record_event(
        _event(candidate_id="dropped"), db_path=database, max_rows=2
    ) is False

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM retrieval_events").fetchone()[0] == 10


def test_existing_oversized_or_symlink_database_is_rejected_for_write(tmp_path, monkeypatch):
    import retrieval_telemetry

    database = tmp_path / "cache/evidence-graph/telemetry.sqlite3"
    retrieval_telemetry.record_event(_event(), db_path=database)
    monkeypatch.setattr(retrieval_telemetry, "MAX_DATABASE_BYTES", 1)
    with pytest.raises(ValueError, match="delete.*regenerate|regenerate.*delete"):
        retrieval_telemetry.record_event(_event(candidate_id="new"), db_path=database)
    assert retrieval_telemetry.best_effort_record_event(
        _event(candidate_id="new"), db_path=database
    ) is False

    monkeypatch.setattr(retrieval_telemetry, "MAX_DATABASE_BYTES", 512 * 1024 * 1024)
    link = tmp_path / "linked.sqlite3"
    try:
        link.symlink_to(database)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises((OSError, PermissionError, ValueError)):
        retrieval_telemetry.record_event(_event(), db_path=link)


def test_insert_rolls_back_when_transactional_page_ceiling_is_exceeded(
    tmp_path, monkeypatch
):
    import retrieval_telemetry

    database = tmp_path / "cache/evidence-graph/telemetry.sqlite3"
    monkeypatch.setattr(retrieval_telemetry, "MAX_DATABASE_BYTES", 1)

    with pytest.raises(ValueError, match="telemetry.*size|size.*telemetry"):
        retrieval_telemetry.record_event(_event(), db_path=database)

    monkeypatch.setattr(retrieval_telemetry, "MAX_DATABASE_BYTES", 512 * 1024 * 1024)
    assert retrieval_telemetry.read_events(limit=10, db_path=database) == []
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_compact_rejects_external_oversized_cache_with_recovery_instruction(
    tmp_path, monkeypatch
):
    import retrieval_telemetry

    database = tmp_path / "cache/evidence-graph/telemetry.sqlite3"
    retrieval_telemetry.record_event(_event(), db_path=database)
    monkeypatch.setattr(retrieval_telemetry, "MAX_DATABASE_BYTES", 1)

    with pytest.raises(ValueError, match="delete.*regenerate|regenerate.*delete"):
        retrieval_telemetry.compact(db_path=database)
