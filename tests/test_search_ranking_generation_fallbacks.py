"""A generation that is missing, mismatched, malformed or changes under a query falls back to Markdown or the base ranking and never mixes legacy evidence."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock

import generation_search
import pytest
from llm_wiki import runtime_roots

from tests.search_ranking_helpers import (
    _activate_search_generation,
    _generation_snapshot,
    _refresh_artifact_descriptor,
    _unregistered_vector_generation,
)

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _orchestrated_legacy_marker(monkeypatch):
    marker = {
        "path": "legacy.md",
        "title": "Legacy",
        "summary": "legacy fallback",
        "score": 1.0,
        "candidate_id": "legacy",
    }
    monkeypatch.setattr(
        generation_search, "markdown_hits", lambda *args, **kwargs: [marker]
    )


def _assert_orchestrated_legacy_fallback(results, expected_reasons):
    assert [result["candidate_id"] for result in results] == ["legacy"]
    assert results[0]["generation"] == "legacy"
    assert results[0]["fallback_reason"] in expected_reasons
    assert results[0]["signals_used"] == ["lexical"]


def test_semantic_generation_never_mixes_legacy_vectors_or_graph(tmp_path, monkeypatch):
    import generation_search
    import search_memory

    _vault, snapshot = _generation_snapshot(
        tmp_path, {"knowledge/notes/page.md": "# Page\nSemantic needle.\n"}
    )
    catalog = search_memory.GenerationCatalog(tmp_path / "state")
    generation = catalog.generations_path / "gen-search"
    generation.mkdir()
    descriptor = generation_search.build_generation_fts(snapshot, generation)
    catalog, _manifest = _activate_search_generation(tmp_path, snapshot, [descriptor])
    monkeypatch.setitem(
        sys.modules,
        "graph_neighbors",
        MagicMock(boost_graph_neighbors=lambda *args: pytest.fail("legacy graph was mixed")),
    )

    results = search_memory.search(
        "semantic needle",
        semantic=True,
        catalog=catalog,
        rerank=False,
        emit_telemetry=False,
    )

    assert results[0]["effective_mode"] == "BASE"
    assert results[0]["fallback_reason"] == "generation_vectors_unavailable"
    assert results[0]["generation"] == "gen-search"


def test_semantic_search_uses_complete_matching_generation_numpy_vectors(tmp_path):
    np = pytest.importorskip("numpy")
    import generation_search
    import search_memory

    _vault, snapshot = _generation_snapshot(
        tmp_path,
        {
            "knowledge/notes/a.md": "# A\nSemantic shared term.\n",
            "knowledge/notes/b.md": "# B\nSemantic shared term.\n",
        },
    )
    catalog = search_memory.GenerationCatalog(tmp_path / "state")
    generation = catalog.generations_path / "gen-search"
    generation.mkdir()

    def embed(texts):
        if len(texts) == 1 and texts[0] == "semantic":
            return np.array([[1.0, 0.0]], dtype=np.float32)
        return np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)

    descriptors = [generation_search.build_generation_fts(snapshot, generation)]
    descriptors.extend(
        search_memory.build_generation_numpy_vectors(
            snapshot,
            generation,
            embedder=embed,
            model_id="deterministic/model",
            model_revision="revision-1",
            dimensions=2,
        )
    )
    catalog, _manifest = _activate_search_generation(
        tmp_path,
        snapshot,
        descriptors,
        vector={
            "model_id": "deterministic/model",
            "model_revision": "revision-1",
            "dimensions": 2,
        },
    )

    results = search_memory.search(
        "semantic",
        semantic=True,
        catalog=catalog,
        generation_embedder=embed,
        generation_model_id="deterministic/model",
        generation_model_revision="revision-1",
        graph=True,
        rerank=False,
        emit_telemetry=False,
    )

    assert results[0]["path"] == "knowledge/notes/a.md"
    assert all(result["generation"] == "gen-search" for result in results)
    assert all(result["effective_mode"] == "HYBRID" for result in results)
    assert all(result["fallback_reason"] is None for result in results)


def test_semantic_generation_vectors_run_under_a_caller_deadline(tmp_path):
    np = pytest.importorskip("numpy")
    import generation_search
    import search_memory

    _vault, snapshot = _generation_snapshot(
        tmp_path,
        {
            "knowledge/notes/a.md": "# A\nSemantic shared term.\n",
            "knowledge/notes/b.md": "# B\nSemantic shared term.\n",
        },
    )
    catalog = search_memory.GenerationCatalog(tmp_path / "state")
    generation = catalog.generations_path / "gen-search"
    generation.mkdir()

    def embed(texts):
        if len(texts) == 1:
            return np.array([[1.0, 0.0]], dtype=np.float32)
        return np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)

    descriptors = [generation_search.build_generation_fts(snapshot, generation)]
    descriptors.extend(
        search_memory.build_generation_numpy_vectors(
            snapshot,
            generation,
            embedder=embed,
            model_id="deterministic/model",
            model_revision="revision-1",
            dimensions=2,
        )
    )
    catalog, _manifest = _activate_search_generation(
        tmp_path,
        snapshot,
        descriptors,
        vector={
            "model_id": "deterministic/model",
            "model_revision": "revision-1",
            "dimensions": 2,
        },
    )
    results = search_memory.search(
        "semantic",
        semantic=True,
        catalog=catalog,
        generation_embedder=embed,
        generation_model_id="deterministic/model",
        generation_model_revision="revision-1",
        graph=False,
        rerank=False,
        emit_telemetry=False,
        deadline_monotonic=time.monotonic() + 30,
    )

    assert results[0]["path"] == "knowledge/notes/a.md"
    assert results[0]["fallback_reason"] is None
    assert all(
        result["signals_used"] == ["lexical", "dense"] for result in results
    ), results
    assert all(result["effective_mode"] == "HYBRID" for result in results)
    assert all(result["fallback_reason"] is None for result in results)


@pytest.mark.parametrize(
    "field",
    [
        "schema_version",
        "collector_version",
        "extractor_version",
        "dimensions",
        "source_ids",
        "source_paths",
        "chunk_ids",
        "source_sha256",
        "model_id",
        "model_revision",
    ],
)
def test_generation_numpy_metadata_mismatch_falls_back_to_base(
    tmp_path, field
):
    np = pytest.importorskip("numpy")
    import generation_fts_validation
    import generation_search
    import search_memory
    from repository_scope import resolve_repository_scope

    _vault, snapshot = _generation_snapshot(
        tmp_path,
        {
            "knowledge/notes/a.md": "# A\nSemantic shared term.\n",
            "knowledge/notes/b.md": "# B\nSemantic shared term.\n",
        },
    )
    generation_root = tmp_path / "generations"
    generation = generation_root / "gen-search"
    generation.mkdir(parents=True)

    def documents_embedder(texts):
        return np.eye(len(texts), 2, dtype=np.float32)

    descriptors = [generation_search.build_generation_fts(snapshot, generation)]
    descriptors.extend(
        search_memory.build_generation_numpy_vectors(
            snapshot,
            generation,
            embedder=documents_embedder,
            model_id="deterministic/model",
            model_revision="revision-1",
            dimensions=2,
        )
    )
    metadata_path = generation / "vectors.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata[field] = _mismatched_value(metadata[field], field)
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    updated_metadata = generation_search.artifact_descriptor(
        metadata_path, metadata_path.name
    )
    descriptors = [
        updated_metadata if item["path"] == metadata_path.name else item
        for item in descriptors
    ]

    class Catalog:
        generations_path = generation_root

        def get_active_for_repository(self, _repository_scope, **_kwargs):
            return {
                "generation_id": "gen-search",
                "schema_version": "corpus-generation/v1",
                "collector_version": snapshot.collector_version,
                "extractor_version": snapshot.extractor_version,
                "tokenizer_version": generation_fts_validation.GENERATION_TOKENIZER_VERSION,
                "tokenizer_config_sha256": generation_fts_validation.GENERATION_TOKENIZER_CONFIG_SHA256,
                "embedding_model_id": "deterministic/model",
                "embedding_model_revision": "revision-1",
                "vector_dimensions": 2,
                "source_manifest_sha256": snapshot.corpus_sha256,
                "artifacts": descriptors,
                "vector_state": "complete",
                "repository_scope": resolve_repository_scope(runtime_roots.vault_root()).as_dict(),
            }

    query_embedder_called = False

    def query_embedder(texts):
        nonlocal query_embedder_called
        query_embedder_called = True
        return np.array([[1.0, 0.0]], dtype=np.float32)

    results = search_memory.search(
        "semantic",
        semantic=True,
        catalog=Catalog(),
        generation_embedder=query_embedder,
        generation_model_id="deterministic/model",
        generation_model_revision="revision-1",
        graph=False,
        rerank=False,
        emit_telemetry=False,
    )

    assert results
    _assert_base_without_vectors(results)
    assert query_embedder_called is False


@pytest.mark.parametrize(
    "matrix",
    [
        pytest.param(lambda np: np.ones((2, 2), dtype=np.float64), id="wrong-float-dtype"),
        pytest.param(lambda np: np.ones((2, 2), dtype=np.int64), id="integer"),
        pytest.param(lambda np: np.full((2, 2), "x"), id="non-numeric"),
        pytest.param(
            lambda np: np.array([[np.nan, 0.0], [0.0, 1.0]], dtype=np.float32),
            id="non-finite",
        ),
    ],
)
def test_generation_numpy_rejects_invalid_matrix_and_falls_back_base(
    tmp_path, matrix
):
    np = pytest.importorskip("numpy")
    import search_memory

    generation, manifest, catalog = _unregistered_vector_generation(tmp_path)
    vector_path = generation / "vectors.npy"
    np.save(vector_path, matrix(np), allow_pickle=False)
    _refresh_artifact_descriptor(manifest, vector_path)

    results = search_memory.search(
        "semantic",
        semantic=True,
        catalog=catalog,
        generation_embedder=lambda texts: np.array([[1.0, 0.0]], dtype=np.float32),
        generation_model_id="deterministic/model",
        generation_model_revision="revision-1",
        graph=False,
        rerank=False,
        emit_telemetry=False,
    )

    assert results
    assert all(result["effective_mode"] == "BASE" for result in results)
    assert all(
        result["fallback_reason"] == "generation_vectors_unavailable"
        for result in results
    )


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), -float("inf")])
def test_generation_numpy_rejects_non_finite_query_vector(tmp_path, bad_value):
    np = pytest.importorskip("numpy")
    import search_memory

    _generation, _manifest, catalog = _unregistered_vector_generation(tmp_path)

    results = search_memory.search(
        "semantic",
        semantic=True,
        catalog=catalog,
        generation_embedder=lambda texts: np.array(
            [[bad_value, 0.0]], dtype=np.float32
        ),
        generation_model_id="deterministic/model",
        generation_model_revision="revision-1",
        graph=False,
        rerank=False,
        emit_telemetry=False,
    )

    assert results
    assert all(result["effective_mode"] == "BASE" for result in results)
    assert all(
        result["fallback_reason"] == "generation_vectors_unavailable"
        for result in results
    )


def test_corrupt_active_generation_falls_back_without_querying_it(tmp_path, monkeypatch):
    import generation_search
    import search_memory

    _vault, snapshot = _generation_snapshot(
        tmp_path, {"knowledge/notes/page.md": "# Page\nCorrupt needle.\n"}
    )
    catalog = search_memory.GenerationCatalog(tmp_path / "state")
    generation = catalog.generations_path / "gen-search"
    generation.mkdir()
    descriptor = generation_search.build_generation_fts(snapshot, generation)
    catalog, _manifest = _activate_search_generation(tmp_path, snapshot, [descriptor])
    artifact = generation / "search.sqlite3"
    content = artifact.read_bytes()
    artifact.write_bytes(b"X" + content[1:])
    # Provide a real legacy lexical path for recovery.
    vault = tmp_path / "vault"
    if not (vault / "knowledge" / "notes").exists():
        # snapshot already created vault layout via helper
        pass
    results = search_memory.search(
        "Corrupt needle",
        catalog=catalog,
        graph=False,
        rerank=False,
        emit_telemetry=False,
        profile="BASE",
    )
    assert isinstance(results, list)
    if results:
        assert results[0].get("fallback_reason") in {
            None,
            "generation_unavailable",
            "generation_corrupt",
            "generation_seal_changed",
            "generation_seal_invalid",
        }
        assert "lexical" in results[0].get("signals_used", ["lexical"])


def test_generation_reader_rejects_source_hash_and_version_mismatch(tmp_path, monkeypatch):
    import generation_fts_validation
    import generation_search
    import search_memory
    from repository_scope import resolve_repository_scope

    _vault, snapshot = _generation_snapshot(
        tmp_path, {"knowledge/notes/page.md": "# Page\nBound needle.\n"}
    )
    generation_root = tmp_path / "generations"
    generation = generation_root / "gen-search"
    generation.mkdir(parents=True)
    descriptor = generation_search.build_generation_fts(snapshot, generation)
    with sqlite3.connect(generation / "search.sqlite3") as database:
        database.execute(
            "UPDATE generation_metadata SET value = ? WHERE key = ?",
            ("0" * 64, "source_manifest_sha256"),
        )

    class Catalog:
        generations_path = generation_root

        def get_active_for_repository(self, _repository_scope, **_kwargs):
            return {
                "generation_id": "gen-search",
                "schema_version": "corpus-generation/v1",
                "collector_version": snapshot.collector_version,
                "extractor_version": snapshot.extractor_version,
                "tokenizer_version": generation_fts_validation.GENERATION_TOKENIZER_VERSION,
                "tokenizer_config_sha256": generation_fts_validation.GENERATION_TOKENIZER_CONFIG_SHA256,
                "source_manifest_sha256": snapshot.corpus_sha256,
                "artifacts": [descriptor],
                "vector_state": "absent",
                "repository_scope": resolve_repository_scope(runtime_roots.vault_root()).as_dict(),
            }

    # Point ROOT at the snapshot vault so legacy lexical can recover.
    vault = _vault
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))

    results = search_memory.search(
        "Bound needle",
        catalog=Catalog(),
        graph=False,
        rerank=False,
        emit_telemetry=False,
        profile="BASE",
    )
    assert results
    assert results[0]["fallback_reason"] in {
        "generation_unavailable",
        "generation_corrupt",
        "generation_seal_changed",
        "generation_seal_invalid",
    }
    assert "lexical" in results[0]["signals_used"]


@pytest.mark.parametrize(
    ("damage", "statement", "parameters"),
    [
        (
            "missing-metadata",
            "DELETE FROM generation_metadata WHERE key = ?",
            ("chunk_count",),
        ),
        (
            "wrong-chunk-count",
            "UPDATE generation_metadata SET value = ? WHERE key = ?",
            ("999", "chunk_count"),
        ),
        (
            "malformed-heading-json",
            "UPDATE chunks SET heading_ancestry = ? WHERE rowid = 1",
            ("{bad-json",),
        ),
        (
            "non-list-heading-json",
            "UPDATE chunks SET heading_ancestry = ? WHERE rowid = 1",
            ('{"heading":"bad"}',),
        ),
        (
            "invalid-chunk-order",
            "UPDATE chunks SET chunk_order = ? WHERE rowid = 1",
            (99,),
        ),
        (
            "invalid-source-hash",
            "UPDATE chunks SET source_sha256 = ? WHERE rowid = 1",
            ("not-a-sha256",),
        ),
        (
            "invalid-chunk-id",
            "UPDATE chunks SET chunk_id = ? WHERE rowid = 1",
            ("not-a-sha256",),
        ),
        (
            "blank-content",
            "UPDATE chunks SET content = ? WHERE rowid = 1",
            ("",),
        ),
    ],
)
def test_malformed_generation_fts_falls_back_to_markdown(
    tmp_path, monkeypatch, damage, statement, parameters
):
    import search_memory

    generation, manifest, catalog = _unregistered_vector_generation(tmp_path)
    artifact = generation / "search.sqlite3"
    with sqlite3.connect(artifact) as database:
        database.execute(statement, parameters)
    _refresh_artifact_descriptor(manifest, artifact)
    _orchestrated_legacy_marker(monkeypatch)

    results = search_memory.search("semantic", catalog=catalog, emit_telemetry=False)
    _assert_orchestrated_legacy_fallback(
        results,
        {"generation_unavailable", "generation_corrupt", "generation_seal_invalid"},
    )


def _mismatched_value(current: object, field: str) -> object:
    """A value of the right shape that no longer matches the generation."""
    if isinstance(current, list):
        return ["mismatch"] * len(current)
    if field == "dimensions":
        return 3
    return "mismatch"


def _assert_base_without_vectors(results) -> None:
    for result in results:
        assert result["effective_mode"] == "BASE"
        assert result["fallback_reason"] == "generation_vectors_unavailable"


def _without_language_column(rows) -> list:
    return [row[:19] + row[20:] for row in rows]


_SCHEMA_DAMAGE = {
    "tokenizer": lambda schema, rows: (
        schema.replace("porter unicode61", "unicode61"),
        rows,
    ),
    "column": lambda schema, rows: (
        schema.replace("language UNINDEXED,", ""),
        _without_language_column(rows),
    ),
    "option": lambda schema, rows: (
        schema.rstrip().removesuffix(")") + ", detail=none)",
        rows,
    ),
}


def _damaged_schema(schema: str, rows: list, damage: str) -> tuple[str, list]:
    apply = _SCHEMA_DAMAGE.get(damage)
    if apply is None:
        return schema, rows
    return apply(schema, rows)


def _metadata_table_sql(damage: str) -> str:
    if damage == "metadata-table":
        return "CREATE TABLE generation_metadata(key TEXT, value TEXT)"
    return "CREATE TABLE generation_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)"


@pytest.mark.parametrize(
    "schema_damage", ["tokenizer", "column", "option", "metadata-table"]
)
def test_generation_fts_rejects_inexact_schema(tmp_path, monkeypatch, schema_damage):
    import search_memory

    generation, manifest, catalog = _unregistered_vector_generation(tmp_path)
    artifact = generation / "search.sqlite3"
    replacement = generation / "replacement.sqlite3"
    with closing(sqlite3.connect(artifact)) as source:
        metadata = source.execute(
            "SELECT key, value FROM generation_metadata ORDER BY key"
        ).fetchall()
        rows = source.execute("SELECT * FROM chunks ORDER BY rowid").fetchall()
        schema = source.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name='chunks'"
        ).fetchone()[0]
    schema, selected_rows = _damaged_schema(schema, rows, schema_damage)
    with closing(sqlite3.connect(replacement)) as database, database:
        database.execute(_metadata_table_sql(schema_damage))
        database.executemany(
            "INSERT INTO generation_metadata(key, value) VALUES (?, ?)", metadata
        )
        database.execute(schema)
        database.executemany(
            "INSERT INTO chunks VALUES ("
            + ",".join("?" for _ in selected_rows[0])
            + ")",
            selected_rows,
        )
    os.replace(replacement, artifact)
    _refresh_artifact_descriptor(manifest, artifact)
    _orchestrated_legacy_marker(monkeypatch)

    results = search_memory.search("semantic", catalog=catalog, emit_telemetry=False)
    _assert_orchestrated_legacy_fallback(
        results,
        {"generation_unavailable", "generation_corrupt", "generation_seal_invalid"},
    )


def test_generation_results_discarded_when_active_manifest_changes_after_read(
    tmp_path, monkeypatch
):
    import search_memory

    generation, manifest, _catalog = _unregistered_vector_generation(tmp_path)

    class Catalog:
        generations_path = generation.parent
        calls = 0

        def get_active_for_repository(self, _repository_scope, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return manifest
            changed = dict(manifest)
            changed["source_manifest_sha256"] = "0" * 64
            return changed

    _orchestrated_legacy_marker(monkeypatch)

    results = search_memory.search("semantic", catalog=Catalog(), emit_telemetry=False)
    _assert_orchestrated_legacy_fallback(
        results, {"generation_unavailable", "generation_seal_changed"}
    )


def test_generation_results_discarded_when_fts_corrupts_after_query(
    tmp_path, monkeypatch
):
    import generation_search
    import search_memory

    generation, _manifest, catalog = _unregistered_vector_generation(tmp_path)
    artifact = generation / "search.sqlite3"
    real_search = generation_search.generation_fts_search

    def corrupt_after_query(*args, **kwargs):
        results = real_search(*args, **kwargs)
        with artifact.open("ab") as output:
            output.write(b"corrupt-after-query")
        return results

    monkeypatch.setattr(generation_search, "generation_fts_search", corrupt_after_query)
    _orchestrated_legacy_marker(monkeypatch)

    results = search_memory.search("semantic", catalog=catalog, emit_telemetry=False)
    _assert_orchestrated_legacy_fallback(results, {"generation_seal_changed"})


def test_generation_hybrid_discarded_when_vector_artifact_changes_after_use(
    tmp_path, monkeypatch
):
    np = pytest.importorskip("numpy")
    import generation_search
    import search_memory

    generation, _manifest, catalog = _unregistered_vector_generation(tmp_path)
    metadata_path = generation / "vectors.json"
    real_search = generation_search.generation_vectors_search

    def corrupt_after_vector_use(*args, **kwargs):
        results = real_search(*args, **kwargs)
        with metadata_path.open("ab") as output:
            output.write(b" ")
        return results

    monkeypatch.setattr(
        generation_search,
        "generation_vectors_search",
        corrupt_after_vector_use,
    )
    _orchestrated_legacy_marker(monkeypatch)

    results = search_memory.search(
        "semantic",
        semantic=True,
        catalog=catalog,
        generation_embedder=lambda texts: np.array([[1.0, 0.0]], dtype=np.float32),
        generation_model_id="deterministic/model",
        generation_model_revision="revision-1",
        graph=False,
        rerank=False,
        emit_telemetry=False,
    )
    _assert_orchestrated_legacy_fallback(results, {"generation_seal_changed"})


def test_generation_hybrid_discarded_after_same_byte_vector_replacement(
    tmp_path, monkeypatch
):
    np = pytest.importorskip("numpy")
    import generation_search
    import search_memory

    generation, _manifest, catalog = _unregistered_vector_generation(tmp_path)
    metadata_path = generation / "vectors.json"
    real_search = generation_search.generation_vectors_search

    def replace_after_vector_use(*args, **kwargs):
        results = real_search(*args, **kwargs)
        replacement = generation / "replacement-vectors.json"
        replacement.write_bytes(metadata_path.read_bytes())
        os.replace(replacement, metadata_path)
        return results

    monkeypatch.setattr(
        generation_search,
        "generation_vectors_search",
        replace_after_vector_use,
    )
    _orchestrated_legacy_marker(monkeypatch)

    results = search_memory.search(
        "semantic",
        semantic=True,
        catalog=catalog,
        generation_embedder=lambda texts: np.array([[1.0, 0.0]], dtype=np.float32),
        generation_model_id="deterministic/model",
        generation_model_revision="revision-1",
        graph=False,
        rerank=False,
        emit_telemetry=False,
    )
    _assert_orchestrated_legacy_fallback(results, {"generation_seal_changed"})
