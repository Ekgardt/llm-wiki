"""Generation artifacts: sealed reads that honour cancellation and deadlines, exact FTS and NumPy builders, exact-filename ranking, filters, the authority hierarchy and the vector path's project boost."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import time
from contextlib import closing, nullcontext
from pathlib import Path

import generation_search
import pytest

from tests.search_ranking_helpers import (
    _activate_search_generation,
    _generation_snapshot,
    _unregistered_vector_generation,
)

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _cancel_after(checks):
    calls = 0

    def cancelled():
        nonlocal calls
        calls += 1
        return calls >= checks

    return cancelled


def test_generation_seal_checks_cancellation_inside_hash_loop(tmp_path):

    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"x" * (128 * 1024))

    with pytest.raises(TimeoutError, match="cancelled"):
        generation_search.sealed_file(artifact, cancelled=_cancel_after(2))


def _run_generation_reader(operation, connection, manifest, catalog) -> None:
    import generation_fts_validation

    if operation == "validation":
        generation_fts_validation.valid_generation_fts(
            connection, manifest, cancelled=_cancel_after(2)
        )
        return
    if operation == "search":
        generation_search.generation_fts_search(
            "semantic",
            manifest,
            connection,
            scope="all",
            limit=10,
            project=None,
            since=None,
            as_of=None,
            cancelled=_cancel_after(2),
        )
        return
    generation_search.generation_vectors_search(
        "semantic",
        catalog,
        manifest,
        connection,
        embedder=lambda _texts: [[1.0, 0.0]],
        model_id="deterministic/model",
        model_revision="revision-1",
        scope="all",
        limit=10,
        project=None,
        since=None,
        as_of=None,
        cancelled=_cancel_after(2),
    )


@pytest.mark.parametrize("operation", ["validation", "search", "vectors"])
def test_generation_readers_check_cancellation_during_work(tmp_path, operation):
    generation, manifest, catalog = _unregistered_vector_generation(tmp_path)
    connection = sqlite3.connect(generation / "search.sqlite3")
    try:
        with pytest.raises(TimeoutError, match="cancelled"):
            _run_generation_reader(operation, connection, manifest, catalog)
    finally:
        connection.close()


def test_generation_connection_uses_remaining_deadline_as_busy_timeout(
    tmp_path, monkeypatch
):
    import generation_fts_validation
    import search_memory

    artifact = tmp_path / "generation" / generation_fts_validation.GENERATION_FTS_ARTIFACT
    artifact.parent.mkdir()
    artifact.touch()
    deadline = time.monotonic() + 0.25
    captured = []

    class Connection:
        def close(self):
            pass

    def connect(*_args, **kwargs):
        captured.append(kwargs)
        return Connection()

    monkeypatch.setattr(search_memory.sqlite3, "connect", connect)
    monkeypatch.setattr(generation_search, "valid_generation_fts", lambda *_a, **_k: True)
    manifest = {
        "generation_id": "generation",
        "artifacts": [{"path": generation_fts_validation.GENERATION_FTS_ARTIFACT}],
    }

    connection = generation_search.generation_connection(
        type("Catalog", (), {"generations_path": tmp_path})(),
        manifest,
        deadline=deadline,
    )

    assert connection is not None
    assert captured[0]["uri"] is True
    assert 0 <= captured[0]["timeout"] <= 0.25


def test_generation_connection_checks_cancel_after_open_before_validation(
    tmp_path, monkeypatch
):
    import generation_fts_validation
    import search_memory

    artifact = tmp_path / "generation" / generation_fts_validation.GENERATION_FTS_ARTIFACT
    artifact.parent.mkdir()
    artifact.touch()
    opened = False
    closed = False

    class Connection:
        def close(self):
            nonlocal closed
            closed = True

    def connect(*_args, **_kwargs):
        nonlocal opened
        opened = True
        return Connection()

    def cancelled():
        return opened

    monkeypatch.setattr(search_memory.sqlite3, "connect", connect)
    monkeypatch.setattr(
        generation_fts_validation,
        "valid_generation_fts",
        lambda *_a, **_k: pytest.fail("validation must not start after cancellation"),
    )
    manifest = {
        "generation_id": "generation",
        "artifacts": [{"path": generation_fts_validation.GENERATION_FTS_ARTIFACT}],
    }

    with pytest.raises(TimeoutError, match="cancelled"):
        generation_search.generation_connection(
            type("Catalog", (), {"generations_path": tmp_path})(),
            manifest,
            deadline=time.monotonic() + 30,
            cancelled=cancelled,
        )

    assert closed is True


def _column(rows, index: int) -> list:
    return [row[index] for row in rows]


def _chunk_field(chunks, name: str) -> list:
    return [getattr(chunk, name) for chunk in chunks]


def test_generation_fts_preserves_exact_snapshot_chunks_and_typed_duplicate_paths(tmp_path):

    vault, snapshot = _generation_snapshot(
        tmp_path,
        {
            "knowledge/notes/concept/same.md": (
                "---\ntype: concept\nsource_authority: user\nconfidence: high\n---\n"
                "# Alpha\nShared needle.\n## Child\nSecond needle.\n"
            ),
            "knowledge/notes/pattern/same.md": (
                "---\ntype: pattern\nsource_authority: web\nconfidence: low\n---\n"
                "# Beta\nShared needle.\n"
            ),
        },
    )
    generation = tmp_path / "generation"
    generation.mkdir()

    descriptor = generation_search.build_generation_fts(snapshot, generation)

    assert descriptor["path"] == "search.sqlite3"
    artifact = generation / descriptor["path"]
    assert descriptor["size"] == artifact.stat().st_size
    assert descriptor["sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()
    with closing(sqlite3.connect(artifact)) as database:
        rows = database.execute(
            "SELECT chunk_id, source_path, heading_ancestry, type, authority, "
            "confidence, source_sha256 FROM chunks ORDER BY rowid"
        ).fetchall()
    assert _column(rows, 0) == _chunk_field(snapshot.chunks, "id")
    assert _column(rows, 1) == _chunk_field(snapshot.chunks, "source_path")
    assert set(_column(rows, 1)) == {
        "knowledge/notes/concept/same.md",
        "knowledge/notes/pattern/same.md",
    }
    assert json.loads(rows[1][2]) == ["Alpha", "Child"]
    assert {tuple(row[3:6]) for row in rows} == {
        ("concept", "user", "high"),
        ("pattern", "web", "low"),
    }
    assert _column(rows, 6) == _chunk_field(snapshot.chunks, "source_sha256")
    assert not (vault / "cache/index.sqlite").exists()


def test_generation_numpy_uses_same_order_and_is_a_real_matrix(tmp_path):
    np = pytest.importorskip("numpy")
    import search_memory

    _vault, snapshot = _generation_snapshot(
        tmp_path,
        {
            "knowledge/notes/a.md": "# A\nFirst vector text.\n## More\nSecond vector text.\n",
            "knowledge/notes/b.md": "# B\nThird vector text.\n",
        },
    )
    generation = tmp_path / "generation"
    generation.mkdir()
    calls = []

    def embed(texts):
        calls.append(tuple(texts))
        return np.arange(len(texts) * 3, dtype=np.float32).reshape(len(texts), 3)

    descriptors = search_memory.build_generation_numpy_vectors(
        snapshot,
        generation,
        embedder=embed,
        model_id="deterministic/model",
        model_revision="revision-1",
        dimensions=3,
    )

    assert calls == [tuple(chunk.text for chunk in snapshot.chunks)]
    assert [item["path"] for item in descriptors] == ["vectors.json", "vectors.npy"]
    matrix = np.load(generation / "vectors.npy", mmap_mode="r")
    metadata = json.loads((generation / "vectors.json").read_text(encoding="utf-8"))
    assert isinstance(matrix, np.ndarray)
    assert matrix.shape == (len(snapshot.chunks), 3)
    assert metadata["chunk_ids"] == [chunk.id for chunk in snapshot.chunks]
    assert metadata["source_sha256"] == [chunk.source_sha256 for chunk in snapshot.chunks]
    assert metadata["corpus_sha256"] == snapshot.corpus_sha256
    assert metadata["collector_version"] == snapshot.collector_version
    assert metadata["extractor_version"] == snapshot.extractor_version


def test_generation_numpy_failure_leaves_both_artifacts_absent(tmp_path):
    pytest.importorskip("numpy")
    import search_memory

    _vault, snapshot = _generation_snapshot(
        tmp_path, {"knowledge/notes/page.md": "# Page\nVector content.\n"}
    )
    generation = tmp_path / "generation"
    generation.mkdir()

    with pytest.raises(RuntimeError, match="embedding failed"):
        search_memory.build_generation_numpy_vectors(
            snapshot,
            generation,
            embedder=lambda texts: (_ for _ in ()).throw(RuntimeError("embedding failed")),
            model_id="model",
            model_revision="rev",
            dimensions=2,
        )

    assert not (generation / "vectors.npy").exists()
    assert not (generation / "vectors.json").exists()
    assert not list(generation.glob(".*.tmp"))


def test_generation_fts_failure_after_publish_removes_artifact(tmp_path, monkeypatch):

    _vault, snapshot = _generation_snapshot(
        tmp_path,
        {"page.md": "# Page\ncleanup term"},
    )
    generation = tmp_path / "generation"
    generation.mkdir()
    monkeypatch.setattr(
        generation_search,
        "artifact_descriptor",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("descriptor cancelled")),
    )

    with pytest.raises(TimeoutError, match="descriptor cancelled"):
        generation_search.build_generation_fts(snapshot, generation)

    assert not (generation / "search.sqlite3").exists()
    assert not list(generation.glob(".*.tmp"))


@pytest.mark.parametrize("builder", ["fts", "vectors"])
def test_generation_builders_refuse_to_overwrite_published_names(tmp_path, builder):
    np = pytest.importorskip("numpy")
    import search_memory

    _vault, snapshot = _generation_snapshot(
        tmp_path, {"knowledge/notes/page.md": "# Page\nImmutable content.\n"}
    )
    generation = tmp_path / "generation"
    generation.mkdir()
    if builder == "fts":
        target = generation / "search.sqlite3"

        def call():
            return generation_search.build_generation_fts(snapshot, generation)
    else:
        target = generation / "vectors.npy"

        def call():
            return search_memory.build_generation_numpy_vectors(
                snapshot,
                generation,
                embedder=lambda texts: np.ones((len(texts), 2), dtype=np.float32),
                model_id="model",
                model_revision="rev",
                dimensions=2,
            )
    target.write_bytes(b"do-not-overwrite")

    with pytest.raises(FileExistsError):
        call()

    assert target.read_bytes() == b"do-not-overwrite"
    assert not list(generation.glob(".*.tmp"))
    if builder == "vectors":
        assert not (generation / "vectors.json").exists()


def test_search_prefers_valid_generation_without_rereading_live_markdown(
    tmp_path, monkeypatch
):
    import search_memory

    _vault, snapshot = _generation_snapshot(
        tmp_path,
        {
            "knowledge/notes/concept/same.md": "# Concept\nGeneration-only needle.\n",
            "knowledge/notes/pattern/same.md": "# Pattern\nGeneration-only needle.\n",
        },
    )
    catalog = search_memory.GenerationCatalog(tmp_path / "state")
    generation = catalog.generations_path / "gen-search"
    generation.mkdir()
    descriptor = generation_search.build_generation_fts(snapshot, generation)
    catalog, _manifest = _activate_search_generation(tmp_path, snapshot, [descriptor])
    monkeypatch.setattr(
        generation_search,
        "collect_pages",
        lambda *args, **kwargs: pytest.fail("generation search reread live Markdown"),
    )

    results = search_memory.search(
        "generation needle",
        catalog=catalog,
        graph=True,
        rerank=False,
        emit_telemetry=False,
    )

    assert [result["path"] for result in results] == [
        "knowledge/notes/concept/same.md",
        "knowledge/notes/pattern/same.md",
    ]
    assert all(result["generation"] == "gen-search" for result in results)
    assert all(result["effective_mode"] == "BASE" for result in results)


def test_generation_exact_filename_is_rank_one_without_fts_content_match(tmp_path):
    import search_memory

    _vault, snapshot = _generation_snapshot(
        tmp_path,
        {
            "knowledge/notes/target-contract.md": (
                "# Unrelated heading\nNo query words occur in this page.\n"
            ),
            "knowledge/notes/distractor.md": (
                "# Target Contract\nTarget contract query words occur here.\n"
            ),
        },
    )
    catalog = search_memory.GenerationCatalog(tmp_path / "state")
    generation = catalog.generations_path / "gen-search"
    generation.mkdir()
    descriptor = generation_search.build_generation_fts(snapshot, generation)
    catalog, _manifest = _activate_search_generation(tmp_path, snapshot, [descriptor])

    results = search_memory.search(
        "target contract",
        catalog=catalog,
        graph=False,
        rerank=False,
        emit_telemetry=False,
    )

    assert results[0]["path"] == "knowledge/notes/target-contract.md"
    assert results[0]["effective_mode"] == "EXACT"
    assert results[0]["generation"] == "gen-search"


def test_generation_exact_filename_uses_canonical_case_and_separator_normalization(
    tmp_path,
):
    import search_memory

    _vault, snapshot = _generation_snapshot(
        tmp_path,
        {
            "knowledge/notes/Target_Contract.md": (
                "# Unrelated heading\nNo query words occur in this page.\n"
            ),
            "knowledge/notes/distractor.md": (
                "# Target Contract\nTarget contract query words occur here.\n"
            ),
        },
    )
    catalog = search_memory.GenerationCatalog(tmp_path / "state")
    generation = catalog.generations_path / "gen-search"
    generation.mkdir()
    descriptor = generation_search.build_generation_fts(snapshot, generation)
    catalog, _manifest = _activate_search_generation(tmp_path, snapshot, [descriptor])

    results = search_memory.search(
        "target contract",
        catalog=catalog,
        graph=False,
        rerank=False,
        emit_telemetry=False,
    )

    assert results[0]["path"] == "knowledge/notes/Target_Contract.md"
    assert results[0]["effective_mode"] == "EXACT"


def test_generation_exact_filename_treats_query_wildcards_literally(tmp_path):
    import search_memory

    _vault, snapshot = _generation_snapshot(
        tmp_path,
        {
            "knowledge/notes/target-extra-contract.md": (
                "# Unrelated heading\nNo query words occur in this page.\n"
            ),
        },
    )
    catalog = search_memory.GenerationCatalog(tmp_path / "state")
    generation = catalog.generations_path / "gen-search"
    generation.mkdir()
    descriptor = generation_search.build_generation_fts(snapshot, generation)
    catalog, _manifest = _activate_search_generation(tmp_path, snapshot, [descriptor])

    results = search_memory.search(
        "target% contract",
        catalog=catalog,
        graph=False,
        rerank=False,
        emit_telemetry=False,
    )

    assert results == []


def test_generation_search_reports_generation_to_telemetry(tmp_path, monkeypatch):
    import retrieval_telemetry
    import search_memory

    _vault, snapshot = _generation_snapshot(
        tmp_path, {"knowledge/notes/page.md": "# Page\nTelemetry needle.\n"}
    )
    catalog = search_memory.GenerationCatalog(tmp_path / "state")
    generation = catalog.generations_path / "gen-search"
    generation.mkdir()
    descriptor = generation_search.build_generation_fts(snapshot, generation)
    catalog, _manifest = _activate_search_generation(tmp_path, snapshot, [descriptor])
    events = []
    monkeypatch.setattr(
        retrieval_telemetry,
        "best_effort_make_event",
        lambda **fields: fields,
    )
    monkeypatch.setattr(
        retrieval_telemetry,
        "best_effort_record_events",
        lambda values: events.extend(values),
    )

    results = search_memory.search(
        "telemetry needle",
        catalog=catalog,
        graph=False,
        rerank=False,
        source_tool="test.generation-search",
    )

    assert len(events) == len(results) == 1
    # One column, one identity: the page's vault-relative path, not the chunk
    # hash. See
    # `docs/research/2026-09-17-one-page-one-identity-and-one-set-of-windows.md`.
    assert (
        events[0]["generation"],
        events[0]["candidate_id"],
        events[0]["retrieval_mode"],
        events[0]["source_tool"],
    ) == ("gen-search", results[0]["path"], "base", "test.generation-search")


def test_generation_filters_use_indexed_authority_status_and_validity(tmp_path):
    import search_memory

    _vault, snapshot = _generation_snapshot(
        tmp_path,
        {
            "knowledge/notes/user.md": (
                "---\ntype: decision\nsource_authority: user\n"
                "valid_from: 2025-01-01\nvalid_to: 2027-01-01\n---\n"
                "# User\nFiltering needle.\n"
            ),
            "knowledge/notes/web.md": (
                "---\ntype: decision\nsource_authority: web\nvalid_to: 2025-01-01\n---\n"
                "# Web\nFiltering needle.\n"
            ),
        },
        # Historical status filtering is exercised through validity metadata below.
    )
    catalog = search_memory.GenerationCatalog(tmp_path / "state")
    generation = catalog.generations_path / "gen-search"
    generation.mkdir()
    descriptor = generation_search.build_generation_fts(snapshot, generation)
    catalog, _manifest = _activate_search_generation(tmp_path, snapshot, [descriptor])

    results = search_memory.search(
        "filtering needle",
        as_of="2026-01-01",
        catalog=catalog,
        graph=False,
        rerank=False,
        emit_telemetry=False,
    )

    assert [result["path"] for result in results] == ["knowledge/notes/user.md"]
    assert results[0]["authority"] == "user"


def test_generation_lexical_ranking_uses_declared_authority_hierarchy(tmp_path):
    import search_memory

    pages = {}
    for authority in ("inferred", "ai-derived", "web", "user"):
        pages[f"knowledge/notes/{authority}.md"] = (
            "---\n"
            "type: concept\n"
            f"source_authority: {authority}\n"
            "---\n"
            "# Shared page\n"
            "Shared ranking needle.\n"
        )
    _vault, snapshot = _generation_snapshot(tmp_path, pages)
    catalog = search_memory.GenerationCatalog(tmp_path / "state")
    generation = catalog.generations_path / "gen-search"
    generation.mkdir()
    descriptor = generation_search.build_generation_fts(snapshot, generation)
    catalog, _manifest = _activate_search_generation(tmp_path, snapshot, [descriptor])

    results = search_memory.search(
        "shared ranking needle",
        catalog=catalog,
        graph=False,
        rerank=False,
        emit_telemetry=False,
    )

    ranked = [(result["authority"], result["score"]) for result in results]
    assert [authority for authority, _score in ranked] == [
        "user", "web", "ai-derived", "inferred"
    ], ranked


def test_missing_or_incompatible_generation_fts_falls_back_to_markdown(
    tmp_path, monkeypatch
):
    import generation_fts_validation
    import search_memory

    class Catalog:
        generations_path = tmp_path / "generations"

        def get_active_for_repository(self, _repository_scope, **_kwargs):
            return {
                "generation_id": "missing",
                "schema_version": "corpus-generation/v1",
                "collector_version": "collector/v1",
                "extractor_version": "extractor/v1",
                "tokenizer_version": generation_fts_validation.GENERATION_TOKENIZER_VERSION,
                "tokenizer_config_sha256": generation_fts_validation.GENERATION_TOKENIZER_CONFIG_SHA256,
                "source_manifest_sha256": "0" * 64,
                "artifacts": [],
                "vector_state": "absent",
            }

    # Behavioral: missing generation still routes through retrieve + lexical legacy.
    vault = tmp_path / "vault"
    notes = vault / "knowledge" / "notes"
    notes.mkdir(parents=True)
    (notes / "page.md").write_text("# Page\nNeedle content.\n", encoding="utf-8")
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    results = search_memory.search(
        "Needle content",
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
        "generation_seal_invalid",
    }
    assert "lexical" in results[0]["signals_used"]


def test_the_vector_path_boosts_a_project_match_by_one_and_a_half(monkeypatch, tmp_path):
    """The lexical ladder doubles a project match; the vector path does not.

    A refactor that shared one boost helper between them silently changed this,
    and no test noticed.
    """
    import generation_fts_validation

    rows = [
        {"chunk_id": "a", "chunk_order": 0, "project": "demo"},
        {"chunk_id": "b", "chunk_order": 1, "project": "other"},
    ]

    class _Rows(list):
        def fetchall(self):
            return list(self)

    class _Connection:
        row_factory = None

        def execute(self, *_args, **_kwargs):
            return _Rows(rows)

    monkeypatch.setattr(generation_search, "generation_filters", lambda **_kwargs: ("", ()))
    monkeypatch.setattr(
        generation_fts_validation,
        "generation_sqlite_guard",
        lambda *_args, **_kwargs: nullcontext(),
    )
    monkeypatch.setattr(
        generation_search, "generation_result", lambda row, _gen, **_options: dict(row)
    )

    scored = generation_search.vector_scored_rows(
        _Connection(),
        [0.4, 0.4],
        "gen-1",
        scope="all",
        since=None,
        as_of=None,
        project="demo",
        deadline=None,
        cancelled=None,
    )

    by_id = {item["chunk_id"]: item["score"] for item in scored}
    assert by_id["a"] == round(0.4 * 1.5, 4)
    assert by_id["b"] == round(0.4, 4)
