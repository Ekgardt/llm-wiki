"""Streaming vectors must survive all three consumers of the same sealed format."""
import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import numpy as np
import pytest
import search_memory as search

from tests.test_generation_rebuild_reuse import (
    _CountingEmbedder,
    _seal_like_a_published_generation,
    _snapshot_from,
)


def _windows_acl_snapshot(path):
    import win32security

    descriptor = win32security.GetNamedSecurityInfo(
        str(path), win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION,
    )
    owner = win32security.ConvertSidToStringSid(descriptor.GetSecurityDescriptorOwner())
    control, _revision = descriptor.GetSecurityDescriptorControl()
    acl = descriptor.GetSecurityDescriptorDacl()
    assert acl is not None, "NULL DACL grants unrestricted access"
    entries = tuple(_windows_ace(acl.GetAce(index)) for index in range(acl.GetAceCount()))
    return owner, bool(control & win32security.SE_DACL_PROTECTED), entries


def _windows_ace(ace):
    import win32security

    assert len(ace) == 3, "unsupported ACE shape"
    (kind, flags), mask, sid = ace
    return kind, flags, mask, win32security.ConvertSidToStringSid(sid)


def _owner_can_use_copy(entry, owner, current_sid):
    _kind, flags, mask, sid = entry
    identities = {owner, current_sid, "S-1-3-4"}
    return sid in identities and mask & 0x001F01FF == 0x001F01FF and not flags & 0x08


def _assert_windows_private_acl(owner, protected, entries, current_sid, require_protected):
    assert owner in {current_sid, "S-1-5-32-544"}
    assert protected or not require_protected
    assert entries
    allowed = {current_sid, "S-1-3-4", "S-1-5-18", "S-1-5-32-544"}
    assert all(entry[0] == 0 and entry[3] in allowed for entry in entries)
    assert any(_owner_can_use_copy(entry, owner, current_sid) for entry in entries)


def _assert_private_copy(captured):
    if os.name != "nt":
        assert captured.parent.stat().st_mode & 0o777 == 0o700
        return
    from operational_ownership import current_actor_identity

    actor = current_actor_identity()
    assert actor.startswith("windows-sid:")
    sid = actor.removeprefix("windows-sid:")
    _assert_windows_private_acl(*_windows_acl_snapshot(captured.parent), sid, True)
    _assert_windows_private_acl(*_windows_acl_snapshot(captured), sid, False)

def _parent(tmp_path):
    snapshot = _snapshot_from(tmp_path / "vault", {"page.md": "# H\nx\n" * 257})
    directory = tmp_path / "parent"
    directory.mkdir()
    search.build_generation_numpy_vectors(
        snapshot, directory, embedder=_CountingEmbedder(), model_id="fixture/model",
        model_revision="rev-1", dimensions=4,
    )
    _seal_like_a_published_generation(directory)
    return snapshot, directory


def test_parent_matrix_is_seal_checked_without_an_eager_matrix_copy(tmp_path):
    snapshot, directory = _parent(tmp_path)
    matrix = search._loaded_parent_matrix(directory, len(snapshot.chunks), 4)
    assert isinstance(matrix, np.memmap)
    assert matrix.shape == (257, 4)
    search._close_vector_matrix(matrix)


def test_vector_metadata_arrays_are_replayable_without_full_lists(tmp_path):
    snapshot, directory = _parent(tmp_path)
    metadata = search._read_vector_metadata(directory, None, None)
    assert not isinstance(metadata["chunk_ids"], list)
    expected = [chunk.id for chunk in snapshot.chunks]
    assert list(metadata["chunk_ids"]) == expected
    assert list(metadata["chunk_ids"]) == expected
    metadata.close()


def test_reuse_does_not_read_the_whole_sealed_artifact(tmp_path, monkeypatch):
    snapshot, directory = _parent(tmp_path)
    original = Path.read_bytes

    def reject_vector_copy(path):
        if path.name in ("vectors.json", "vectors.npy"):
            raise AssertionError("whole vector artifact copied")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", reject_vector_copy)
    destination = tmp_path / "child"
    destination.mkdir()
    embedder = _CountingEmbedder()
    search.build_generation_numpy_vectors(
        snapshot, destination, embedder=embedder, model_id="fixture/model",
        model_revision="rev-1", dimensions=4, reuse_from=directory,
    )
    assert embedder.encoded == []


def test_build_never_allocates_the_full_matrix_in_numpy_heap(tmp_path, monkeypatch):
    snapshot, _directory = _parent(tmp_path)
    original = np.zeros
    requested = []

    def observe_allocation(shape, *args, **kwargs):
        requested.append(shape)
        return original(shape, *args, **kwargs)

    monkeypatch.setattr(np, "zeros", observe_allocation)
    destination = tmp_path / "fresh"
    destination.mkdir()
    search.build_generation_numpy_vectors(
        snapshot, destination, embedder=_CountingEmbedder(), model_id="fixture/model",
        model_revision="rev-1", dimensions=4,
    )
    assert (257, 4) not in requested
    assert json.loads((destination / "vectors.json").read_text())["chunk_ids"]


def test_private_parent_copy_survives_original_truncation_and_is_removed(tmp_path):
    snapshot, directory = _parent(tmp_path)
    matrix = search._loaded_parent_matrix(directory, len(snapshot.chunks), 4)
    captured = Path(matrix.filename)
    expected = matrix.copy()
    (directory / "vectors.npy").write_bytes(b"")
    assert np.array_equal(matrix, expected)
    assert captured != directory / "vectors.npy"
    _assert_private_copy(captured)
    search._close_vector_matrix(matrix)
    assert not captured.parent.exists()


def test_metadata_uses_exact_copied_bytes_even_if_original_changes(tmp_path, monkeypatch):
    _snapshot, directory = _parent(tmp_path)
    parse = search._parse_vector_metadata

    def change_original_then_parse(path, check_stop):
        original = directory / "vectors.json"
        value = json.loads(original.read_text())
        value["model_id"] = "different/model"
        original.write_text(json.dumps(value))
        return parse(path, check_stop)

    monkeypatch.setattr(search, "_parse_vector_metadata", change_original_then_parse)
    with closing(search._parent_vector_metadata(directory)) as metadata:
        assert metadata["model_id"] == "fixture/model"


def test_source_mutation_during_copy_refuses_reuse(tmp_path):
    _snapshot, directory = _parent(tmp_path)
    mutations = []

    def mutate_during_copy():
        if not mutations:
            mutations.append(True)
            (directory / "vectors.json").write_text("{}")

    assert search._parent_vector_metadata(directory, mutate_during_copy) is None
    assert mutations == [True]


@pytest.mark.parametrize("suffix", (" trailing", ",", "\n[]"))
def test_metadata_trailing_or_truncated_json_is_refused(tmp_path, suffix):
    _snapshot, directory = _parent(tmp_path)
    path = directory / "vectors.json"
    path.write_text(path.read_text() + suffix)
    with pytest.raises(ValueError):
        search._read_vector_metadata(directory, None, None)


def test_duplicate_ids_and_parallel_array_mismatch_are_refused(tmp_path):
    _snapshot, directory = _parent(tmp_path)
    path = directory / "vectors.json"
    value = json.loads(path.read_text())
    original_id = value["chunk_ids"][1]
    value["chunk_ids"][1] = value["chunk_ids"][0]
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="repeated"):
        search._read_vector_metadata(directory, None, None)
    value["chunk_ids"][1] = original_id
    value["chunk_ids"].pop()
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        search._read_vector_metadata(directory, None, None)


def test_expired_metadata_clock_and_cancelled_builder_keep_no_artifacts(tmp_path):
    snapshot, directory = _parent(tmp_path)
    with pytest.raises(TimeoutError):
        search._read_vector_metadata(directory, time.monotonic() - 1, None)
    destination = tmp_path / "cancelled"
    destination.mkdir()

    def refuse():
        raise TimeoutError("controlled caller expired")

    with pytest.raises(TimeoutError):
        search._built_generation_vectors(snapshot, destination, embedder=_CountingEmbedder(),
                                        model_id="fixture/model", model_revision="rev-1",
                                        dimensions=4, check_stop=refuse)
    assert list(destination.iterdir()) == []


def test_missing_legacy_extractor_keeps_only_exact_id_reuse(tmp_path):
    snapshot, directory = _parent(tmp_path)
    path = directory / "vectors.json"
    value = json.loads(path.read_text())
    del value["extractor_version"]
    path.write_text(json.dumps(value))
    _seal_like_a_published_generation(directory)
    destination = tmp_path / "legacy-child"
    destination.mkdir()
    embedder = _CountingEmbedder()
    search.build_generation_numpy_vectors(snapshot, destination, embedder=embedder,
                                         model_id="fixture/model", model_revision="rev-1",
                                         dimensions=4, reuse_from=directory)
    assert embedder.encoded == []


class _NoFetchallRows:
    def __init__(self, cursor):
        self.cursor = cursor

    def __iter__(self):
        return iter(self.cursor)

    def fetchone(self):
        return self.cursor.fetchone()

    def fetchall(self):
        raise AssertionError("identity or scored prose retained every row")

    def close(self):
        self.cursor.close()


class _NoFetchallConnection(sqlite3.Connection):
    def execute(self, *args, **kwargs):
        return _NoFetchallRows(super().execute(*args, **kwargs))


def test_reader_identity_and_dense_admission_do_not_fetch_all_prose(tmp_path):
    snapshot, directory = _parent(tmp_path)
    search.build_generation_fts(snapshot, directory)
    with closing(sqlite3.connect(directory / "search.sqlite3", factory=_NoFetchallConnection)) as database:
        database.row_factory = sqlite3.Row
        ordered = search._ordered_chunk_identity(database, None, None)
        assert len(ordered) == len(snapshot.chunks)
        assert len(list(ordered)) == len(snapshot.chunks)
        actual = _scored(database, np.arange(len(ordered), dtype=np.float32), limit=2)
    assert len(actual) == 2


def _scored(database, scores, **options):
    return search._vector_scored_rows(database, scores, "fixture-generation", scope="all",
                                     since=None, as_of=None, project=None,
                                     deadline=None, cancelled=None, **options)


def test_bounded_dense_tiers_equal_the_full_historical_ranked_admission(tmp_path):
    snapshot, directory = _parent(tmp_path)
    search.build_generation_fts(snapshot, directory)
    with closing(sqlite3.connect(directory / "search.sqlite3")) as database:
        database.row_factory = sqlite3.Row
        scores = np.arange(len(snapshot.chunks), dtype=np.float32)
        expected = search._admit_source_tiers(_scored(database, scores), 2)
        assert _scored(database, scores, limit=2) == expected
        database.execute("UPDATE chunks SET source_path='knowledge/raw/sessions/fixture.md' WHERE chunk_order IN (1,2)")
        database.commit()
        expected = search._admit_source_tiers(_scored(database, scores), 2)
        assert len(expected) == 4
        assert _scored(database, scores, limit=2) == expected


def test_cosine_working_scores_hold_one_block_not_the_full_array():
    matrix = np.ones((4097, 4), dtype=np.float32)
    scores = search._cosine_similarities(matrix, np.ones(4), deadline=None, cancelled=None)
    assert not isinstance(scores, np.ndarray)
    assert scores[0] == pytest.approx(1.0)
    assert len(scores.block) == 4096
    assert scores[-1] == pytest.approx(1.0)
    assert len(scores.block) == 1


def _vector_manifest(snapshot, directory):
    return {
        "source_manifest_sha256": snapshot.corpus_sha256,
        "collector_version": snapshot.collector_version,
        "extractor_version": snapshot.extractor_version,
        "vector_dimensions": 4,
        "artifacts": [search._artifact_descriptor(directory / name, name)
                      for name in search.GENERATION_VECTOR_ARTIFACTS],
    }


def test_dense_reader_never_maps_the_mutable_generation_file(tmp_path, monkeypatch):
    snapshot, directory = _parent(tmp_path)
    search.build_generation_fts(snapshot, directory)
    manifest = _vector_manifest(snapshot, directory)
    original_load = np.load

    def truncate_original_after_private_map(path, *args, **kwargs):
        assert Path(path) != directory / "vectors.npy", "reader mapped the mutable original"
        matrix = original_load(path, *args, **kwargs)
        (directory / "vectors.npy").write_bytes(b"")
        return matrix

    monkeypatch.setattr(np, "load", truncate_original_after_private_map)
    with closing(sqlite3.connect(directory / "search.sqlite3")) as database:
        database.row_factory = sqlite3.Row
        actual = search._generation_vector_rows("query", database, manifest, directory,
            "fixture-generation", embedder=_CountingEmbedder(), model_id="fixture/model",
            model_revision="rev-1", scope="all", limit=2, project=None,
            since=None, as_of=None, deadline=None, cancelled=None)
    assert actual


def test_cancelled_dense_copy_removes_owned_scratch_and_preserves_source(tmp_path, monkeypatch):
    snapshot, directory = _parent(tmp_path)
    manifest = _vector_manifest(snapshot, directory)
    expected = (directory / "vectors.npy").read_bytes()
    original = search.tempfile.TemporaryDirectory
    created, calls = [], []

    def observed_temporary(*args, **kwargs):
        temporary = original(*args, **kwargs)
        created.append(Path(temporary.name))
        return temporary

    def cancel_after_copy_begins():
        calls.append(True)
        return len(calls) >= 3

    monkeypatch.setattr(search.tempfile, "TemporaryDirectory", observed_temporary)
    with pytest.raises(TimeoutError):
        search._loaded_generation_vector_matrix(directory, manifest, len(snapshot.chunks),
                                                None, cancel_after_copy_begins)
    assert created
    assert all(not path.exists() for path in created)
    assert (directory / "vectors.npy").read_bytes() == expected


def test_reader_copied_metadata_must_match_the_manifest_digest(tmp_path):
    _snapshot, directory = _parent(tmp_path)
    expected = search._artifact_descriptor(directory / "vectors.json", "vectors.json")
    value = json.loads((directory / "vectors.json").read_text())
    value["model_id"] = "altered/model"
    (directory / "vectors.json").write_text(json.dumps(value))
    with pytest.raises(ValueError, match="manifest"):
        search._read_vector_metadata(directory, None, None, expected=expected)


def test_ordered_identity_prepares_an_independent_connection_for_named_dense_rows(tmp_path):
    snapshot, directory = _parent(tmp_path)
    search.build_generation_fts(snapshot, directory)
    with sqlite3.connect(directory / search.GENERATION_FTS_ARTIFACT) as connection:
        assert connection.row_factory is None
        ordered = search._ordered_chunk_identity(connection, time.monotonic() + 30, None)
        assert len(ordered) == 257
        row = connection.execute("SELECT chunk_order FROM chunks ORDER BY chunk_order LIMIT 1").fetchone()
        assert row["chunk_order"] == 0
