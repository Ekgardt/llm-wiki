"""All identities are checked without repeated Python/SQLite crossings."""
import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing

import pytest
import search_memory as search

from tests.test_vectors_stream_through_build_reuse_and_read import _parent


class ObservedConnection(sqlite3.Connection):
    crossings = None

    def execute(self, sql, *args, **kwargs):
        if sql.startswith("INSERT INTO vector_values"):
            self.crossings.append("execute")
        return super().execute(sql, *args, **kwargs)

    def executemany(self, sql, parameters):
        assert isinstance(parameters, Iterator), "parameter rows must remain lazy"
        self.crossings.append("executemany")
        return super().executemany(sql, parameters)


def test_each_metadata_array_crosses_the_driver_once(tmp_path, monkeypatch):
    snapshot, directory = _parent(tmp_path)
    original = search.sqlite3.connect
    crossings = []

    def connect(path):
        database = original(path, factory=ObservedConnection)
        database.crossings = crossings
        return database

    monkeypatch.setattr(search.sqlite3, "connect", connect)
    with closing(search._read_vector_metadata(directory, None, None)) as metadata:
        assert list(metadata["chunk_ids"]) == [chunk.id for chunk in snapshot.chunks]
    assert crossings == ["executemany"] * len(search.VECTOR_ARRAY_FIELDS)


class OrderedIdentities:
    def __init__(self, rows):
        self.rows, self.walks = rows, 0

    def __len__(self):
        return len(self.rows)

    def __iter__(self):
        self.walks += 1
        return iter(self.rows)


def test_all_identity_fields_share_one_ordered_walk():
    rows = [("chunk-a", "source-a", "page-a.md", "a" * 64),
            ("chunk-b", "source-b", "page-b.md", "b" * 64)]
    ordered = OrderedIdentities(rows)
    metadata = {field: [row[index] for row in rows]
                for index, field in enumerate(search.VECTOR_ARRAY_FIELDS)}
    assert search._vector_identity_arrays_match(metadata, ordered)
    assert ordered.walks == 1


@pytest.mark.parametrize("field", search.VECTOR_ARRAY_FIELDS)
def test_a_change_in_any_identity_field_still_refuses(field):
    rows = [("chunk-a", "source-a", "page-a.md", "a" * 64)]
    metadata = {name: [row[index] for row in rows]
                for index, name in enumerate(search.VECTOR_ARRAY_FIELDS)}
    metadata[field] = ["altered"]
    assert not search._vector_identity_arrays_match(metadata, OrderedIdentities(rows))


def test_duplicate_after_many_members_still_refuses(tmp_path):
    _snapshot, directory = _parent(tmp_path)
    path = directory / "vectors.json"
    metadata = json.loads(path.read_text())
    metadata["chunk_ids"][-1] = metadata["chunk_ids"][0]
    path.write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="repeated"):
        search._read_vector_metadata(directory, None, None)
