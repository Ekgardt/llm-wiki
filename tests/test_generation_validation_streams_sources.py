"""Source rederivation must not retain the entire corpus before comparison."""

import hashlib
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import pytest  # noqa: E402
import search_memory  # noqa: E402


def test_expected_rows_derive_only_the_source_being_consumed(monkeypatch):
    visited = []

    def chunks(source_id, source, **kwargs):
        visited.append(source_id)
        yield source_id

    monkeypatch.setattr(search_memory, "_expected_source_chunks", chunks)
    monkeypatch.setattr(search_memory, "_generation_chunk_row", lambda chunk, order: (chunk, order))
    sources = {
        f"source:{number:04}": {"relative_path": f"{number:04}", "content": b"x"}
        for number in range(100)
    }
    rows = search_memory._expected_chunk_rows(sources, manifest={}, deadline=None, cancelled=None)
    assert visited == []
    iterator = iter(rows)
    assert next(iterator) == ("source:0000", 0)
    assert visited == ["source:0000"]
    assert len(list(iterator)) == 99


def test_stored_count_has_no_global_chunk_ceiling():
    with sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE TABLE chunks (value)")
        connection.executemany("INSERT INTO chunks VALUES (?)", ((n,) for n in range(100_002)))
        assert search_memory._stored_chunk_count(connection) == 100_002


@pytest.mark.parametrize("expected_count", [0, 1, 3])
def test_previously_checked_rows_still_require_the_exact_source_count(expected_count):
    assert not search_memory._stored_chunks_match(
        None,
        iter(range(expected_count)),
        count=2,
        check_rows=False,
        deadline=None,
        cancelled=None,
    )


def test_source_comparison_rejects_extra_and_missing_rows():
    expected = iter([("first",), ("second",)])
    assert not search_memory._chunk_row_mismatch(("first",), expected)
    assert not search_memory._expected_rows_end(expected)
    assert search_memory._chunk_row_mismatch(("extra",), iter(()))


def test_unusable_source_content_is_rejected_before_rederivation():
    assert (
        search_memory._expected_chunk_rows(
            {"source:bad": {"relative_path": "bad", "content": None}},
            manifest={},
            deadline=None,
            cancelled=None,
        )
        is None
    )


def _stored_row(identity, order):
    digest = hashlib.sha256(identity.encode()).hexdigest()
    path = "knowledge/notes/example.md"
    return (digest, order, f"source:{path}", path, digest, path, '["Example"]',
            0, 3, 1, 1, digest, "concept", None, "user", "high", "active",
            None, None, None, "Example", "abc", "")


class _UnboundedIdSet(set):
    def add(self, value):
        super().add(value)
        assert len(self) <= 2, "validation retains the entire corpus ID set"


def test_chunk_validation_does_not_retain_the_corpus_id_set(monkeypatch):
    monkeypatch.setattr(search_memory, "set", _UnboundedIdSet, raising=False)
    with sqlite3.connect(":memory:") as connection:
        connection.executescript(search_memory._GENERATION_FTS_DDL)
        connection.executemany(
            "INSERT INTO chunks VALUES (" + ",".join("?" for _ in range(23)) + ")",
            (_stored_row(str(index), index) for index in range(4)),
        )
        assert search_memory._rows_hold_invariants(
            connection, None, count=4, deadline=None, cancelled=None
        )


def test_duplicate_chunk_ids_remain_invalid():
    with sqlite3.connect(":memory:") as connection:
        connection.executescript(search_memory._GENERATION_FTS_DDL)
        connection.executemany(
            "INSERT INTO chunks VALUES (" + ",".join("?" for _ in range(23)) + ")",
            (_stored_row("same", index) for index in range(2)),
        )
        assert not search_memory._rows_hold_invariants(
            connection, None, count=2, deadline=None, cancelled=None
        )
