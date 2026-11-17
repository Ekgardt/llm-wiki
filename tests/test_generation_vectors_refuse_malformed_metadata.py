from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import generation_search
import numpy as np
import pytest
from corpus_snapshot import collect_corpus

search: Any = generation_search

VALID_METADATA = {
    "schema_version": "corpus-vectors/v1",
    "model_id": "model",
    "model_revision": "revision",
    "dimensions": 2,
    "chunk_ids": ["a", "b"],
    "source_ids": ["s", "s"],
}


class _PassageEncoder:
    def encode(self, texts: list[str]) -> np.ndarray[Any, np.dtype[np.float32]]:
        return np.ones((len(texts), 2), dtype=np.float32)


def _written(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "vectors.json"
    path.write_text(text, encoding="utf-8")
    return path


def _snapshot(tmp_path: Path) -> Any:
    notes = tmp_path / "vault" / "knowledge" / "notes"
    notes.mkdir(parents=True)
    page = "---\ntype: concept\n---\n# Lantern\n\nA page about lanterns.\n"
    (notes / "lantern.md").write_text(page, encoding="utf-8")
    return collect_corpus(tmp_path / "vault", max_files=10)


def test_valid_vector_metadata_reads_back_as_columns(tmp_path: Path) -> None:
    metadata = search.parse_vector_metadata(_written(tmp_path, json.dumps(VALID_METADATA)), None)
    try:
        assert len(metadata) == len(VALID_METADATA)
        assert list(metadata["chunk_ids"]) == ["a", "b"]
        assert metadata["chunk_ids"][-1] == "b"
        reusable = search._ReusableVectors(metadata, np.zeros((2, 2), dtype=np.float32))
        assert len(reusable) == 2
        assert list(reusable) == ["a", "b"]
    finally:
        metadata.close()


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[]", "invalid vector metadata JSON framing"),
        ('{"chunk_ids": [["a"]]}', "requires flat scalar values"),
        ('{"model_id": tru}', "invalid vector metadata JSON scalar"),
        ('{"chunk_ids": [1]}', "array member must be a string"),
        ('{"model_id": ""}', "header must be a nonempty string"),
        ('{"model_id": "a", "model_id": "a"}', "key is invalid or repeated"),
        ('{5: "a"}', "key is invalid or repeated"),
        ('{"unknown": "a"}', "unknown vector metadata field"),
        ('{"model_id": "a"}', "cache identity fields are incomplete"),
    ],
)
def test_malformed_vector_metadata_is_refused(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        search.parse_vector_metadata(_written(tmp_path, text), None)


def test_parent_metadata_past_its_byte_bound_is_not_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _written(tmp_path, json.dumps(VALID_METADATA))
    (tmp_path / "vectors.npy").write_bytes(b"matrix")
    monkeypatch.setattr(generation_search, "MAX_PARENT_VECTOR_METADATA_BYTES", 1)
    assert search.parent_vector_metadata(tmp_path) is None


def test_a_manifest_that_is_not_an_object_is_not_a_mapping(tmp_path: Path) -> None:
    listed = tmp_path / "manifest.json"
    listed.write_text("[1, 2]", encoding="utf-8")
    assert generation_search._loaded_json_mapping(listed) is None


def test_a_non_finite_parent_matrix_offers_nothing_to_reuse(tmp_path: Path) -> None:
    metadata = search.parse_vector_metadata(_written(tmp_path, json.dumps(VALID_METADATA)), None)
    matrix = np.array([[np.nan, 0.0], [0.0, 0.0]], dtype=np.float32)
    assert search._parent_rows_or_empty(metadata, matrix, None) == {}


def test_text_that_cannot_be_encoded_breaks_the_reuse_identity() -> None:
    chunk = type("Chunk", (), {"text": "\ud800", "span_sha256": "", "id": ""})()
    assert not search._chunk_reuse_identity_holds(chunk, "extractor")
    unencodable = {"extractor_version": "\ud800"}
    assert generation_search._parent_cache_extractor(unencodable, "current") == "current"


def test_an_embedder_without_encode_is_refused() -> None:
    with pytest.raises(TypeError, match="callable or provide encode"):
        search._call_generation_embedder(object(), ["text"])


@pytest.mark.parametrize("value", [True, 0, "1"])
def test_a_positive_integer_is_required(value: object) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        generation_search._require_positive_int(value, "must be positive")


def test_vector_inputs_need_a_snapshot_and_a_named_model(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="snapshot must be a CorpusSnapshot"):
        generation_search._require_vector_build_inputs(object(), "model", "revision", 2)
    with pytest.raises(TypeError, match="snapshot must be a CorpusSnapshot"):
        generation_search._require_buildable_snapshot(object(), None, None)
    with pytest.raises(ValueError, match="model ID and revision must be non-empty"):
        generation_search._require_vector_build_inputs(_snapshot(tmp_path), "", "revision", 2)


@pytest.mark.parametrize(
    ("matrix", "message"),
    [
        (np.zeros((1, 2), dtype=np.float32), "incompatible shape"),
        (np.array([[np.nan, 1.0, 1.0]], dtype=np.float32), "non-finite numeric matrix"),
    ],
)
def test_an_embedded_matrix_of_the_wrong_shape_or_values_is_refused(
    matrix: np.ndarray[Any, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        search._require_embedded_shape(matrix, 1, 3)


def test_an_empty_corpus_has_an_empty_vector_matrix(tmp_path: Path) -> None:
    matrix = search._new_vector_matrix(tmp_path / "vectors.npy", 0, 3)
    assert matrix.shape == (0, 3)


def test_a_changed_chunk_count_is_refused() -> None:
    with pytest.raises(ValueError, match="vector source chunk count changed"):
        search._require_vector_row_count(1, 2)


def test_chunk_fields_read_as_a_sequence(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    identifiers = search._ChunkVectorField(snapshot, "id")
    assert len(identifiers) == len(snapshot.chunks)
    assert list(identifiers) == [chunk.id for chunk in snapshot.chunks]
    assert identifiers[0] == snapshot.chunks[0].id
    assert list(search._vector_array_values(["a"], None)) == ["a"]


def test_an_in_memory_matrix_is_saved_whole(tmp_path: Path) -> None:
    target = tmp_path / "copy.npy"
    matrix = np.ones((2, 2), dtype=np.float32)
    search._copy_vector_matrix(target, matrix, None)
    assert np.array_equal(np.load(target), matrix)


def test_publishing_a_missing_temporary_file_fails_without_leftovers(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        generation_search._publish_new_file(tmp_path / "missing.tmp", tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_generation_output_must_be_a_real_directory(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(PermissionError, match="existing regular directory"):
        generation_search._generation_directory(link)


def test_the_passage_encoder_prefixes_each_text_as_a_passage() -> None:
    encode = search._generation_embedder(_PassageEncoder(), is_query=False)
    assert encode(["one", "two"]) == [[1.0, 1.0], [1.0, 1.0]]


def test_lazy_encoders_use_the_loaded_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(generation_search, "embedder_cache", _PassageEncoder())
    assert search.lazy_generation_query_encoder()(["question"]) == [[1.0, 1.0]]
    assert search._lazy_passage_encoder()(["passage"]) == [[1.0, 1.0]]


def test_a_cached_embedder_is_returned_without_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    cached = _PassageEncoder()
    monkeypatch.setattr(generation_search, "embedder_cache", cached)
    assert search.get_embedder() is cached


def test_a_loaded_embedder_clears_the_previous_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import onnx_encoder

    loaded = _PassageEncoder()
    monkeypatch.setattr(generation_search, "embedder_cache", None)
    monkeypatch.setattr(generation_search, "embedder_failure_reason", "earlier failure")
    monkeypatch.setattr(onnx_encoder, "load_encoder", lambda _model, _revision: loaded)
    assert search.get_embedder() is loaded
    assert generation_search.embedder_failure_reason is None


@pytest.mark.parametrize(
    ("matrix", "dimensions"),
    [
        (np.zeros((1, 2), dtype=np.float32), 3),
        (np.zeros((1, 2), dtype=np.float64), 2),
    ],
)
def test_a_stored_matrix_of_the_wrong_shape_or_type_is_unusable(
    matrix: np.ndarray[Any, Any], dimensions: int
) -> None:
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.row_factory = sqlite3.Row
        ordered = [connection.execute("SELECT 'a' AS chunk_id").fetchone()]
    assert not generation_search._usable_vector_matrix(
        matrix, ordered, dimensions, deadline=None, cancelled=None
    )


@pytest.mark.parametrize(
    "query",
    [np.zeros((1, 3), dtype=np.float32), np.zeros((1, 2), dtype=np.int64)],
)
def test_a_query_vector_of_the_wrong_shape_or_type_is_unusable(
    query: np.ndarray[Any, Any],
) -> None:
    assert not generation_search._usable_query_vector(query, 2)


def test_vector_identity_needs_every_parallel_column() -> None:
    assert not search.vector_identity_arrays_match({}, [("a", "s", "p", "h")])


def test_no_vector_rows_are_admitted_without_room() -> None:
    assert search._vector_admission([("row",)], 0, True) == []


def test_ordered_vector_identities_are_indexed_like_a_sequence() -> None:
    with closing(sqlite3.connect(":memory:")) as database:
        database.execute(
            "CREATE TABLE chunks (chunk_id, source_id, source_path, source_sha256, chunk_order)"
        )
        database.executemany(
            "INSERT INTO chunks VALUES (?, 's', 'p', 'h', ?)", [("a", 0), ("b", 1)]
        )
        ordered = search._OrderedVectorIdentity(database, None, None)
        assert len(ordered) == 2
        assert ordered[-1] == ("b", "s", "p", "h")
        assert [row[0] for row in ordered] == ["a", "b"]
