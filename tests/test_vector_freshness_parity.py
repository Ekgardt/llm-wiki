"""Task 12: vector freshness, Lance distance conversion, filter parity."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def test_lance_distance_converted_to_named_distance_and_similarity() -> None:
    import lance_store

    rows = lance_store._rows_from_lance_hits(
        [
            {
                "path": "a.md",
                "title": "A",
                "summary": "s",
                "project": "demo",
                "timestamp": "2026-01-01",
                "_distance": 0.25,
            },
            {
                "path": "b.md",
                "title": "B",
                "summary": "t",
                "project": "other",
                "timestamp": "2025-01-01",
                "_distance": 1.0,
            },
        ]
    )
    assert rows[0]["lance_distance"] == 0.25
    assert rows[0]["vector_score"] == pytest.approx(1.0 / (1.0 + 0.25))
    assert rows[0]["score"] == rows[0]["vector_score"]
    assert rows[0]["vector_score"] > rows[1]["vector_score"]
    assert "distance" not in rows[0] or rows[0].get("lance_distance") == 0.25


def test_lance_and_numpy_apply_identical_hard_filters() -> None:
    import lance_store

    rows = [
        {
            "path": "a.md",
            "title": "A",
            "summary": "s",
            "project": "demo",
            "timestamp": "2026-06-01",
            "status": "active",
            "valid_from": "2026-01-01",
            "valid_to": "",
            "score": 0.9,
            "lance_distance": 0.1,
            "vector_score": 0.9,
        },
        {
            "path": "b.md",
            "title": "B",
            "summary": "t",
            "project": "other",
            "timestamp": "2024-01-01",
            "status": "superseded",
            "valid_from": "2020-01-01",
            "valid_to": "2025-01-01",
            "score": 0.95,
            "lance_distance": 0.05,
            "vector_score": 0.95,
        },
        {
            "path": "c.md",
            "title": "C",
            "summary": "u",
            "project": "demo",
            "timestamp": "2026-03-01",
            "status": "active",
            "valid_from": "2026-01-01",
            "valid_to": "",
            "score": 0.8,
            "lance_distance": 0.2,
            "vector_score": 0.8,
        },
    ]
    filtered = lance_store.apply_vector_filters(
        rows,
        project="demo",
        since="2026-01-01",
        as_of="2026-07-01",
    )
    assert [r["path"] for r in filtered] == ["a.md", "c.md"]


def test_stale_vector_state_refuses_dense_with_base_fallback(tmp_path, monkeypatch) -> None:
    np = pytest.importorskip("numpy")
    import search_memory

    vault = tmp_path / "vault"
    notes = vault / "knowledge" / "notes"
    notes.mkdir(parents=True)
    (notes / "page.md").write_text("# Page\nStale vector needle.\n", encoding="utf-8")
    monkeypatch.setattr(search_memory, "ROOT", vault)
    monkeypatch.setattr(search_memory, "KNOWLEDGE_DIR", notes)
    monkeypatch.setattr(search_memory, "WIKI_DIR", notes)
    monkeypatch.setattr(search_memory, "INDEX_DIR", tmp_path / "legacy-cache")
    monkeypatch.setattr(
        search_memory, "INDEX_FILE", tmp_path / "legacy-cache" / "index.sqlite"
    )
    monkeypatch.setattr(
        search_memory,
        "INDEX_MANIFEST",
        tmp_path / "legacy-cache" / ".paths-manifest",
    )

    from corpus_snapshot import collect_corpus

    snapshot = collect_corpus(vault)
    catalog = search_memory.GenerationCatalog(tmp_path / "state")
    generation = catalog.generations_path / "gen-stale"
    generation.mkdir(parents=True)
    descriptors = [search_memory.build_generation_fts(snapshot, generation)]
    descriptors.extend(
        search_memory.build_generation_numpy_vectors(
            snapshot,
            generation,
            embedder=lambda texts: np.ones((len(texts), 2), dtype=np.float32),
            model_id="m",
            model_revision="r1",
            dimensions=2,
        )
    )
    manifest = {
        "generation_id": "gen-stale",
        "schema_version": "corpus-generation/v1",
        "collector_version": snapshot.collector_version,
        "extractor_version": snapshot.extractor_version,
        "tokenizer_version": search_memory.GENERATION_TOKENIZER_VERSION,
        "tokenizer_config_sha256": search_memory.GENERATION_TOKENIZER_CONFIG_SHA256,
        "embedding_model_id": "m",
        "embedding_model_revision": "r1",
        "vector_dimensions": 2,
        "source_manifest_sha256": snapshot.corpus_sha256,
        "artifacts": descriptors,
        "vector_state": "stale",
        "repository_scope": __import__("repository_scope").resolve_repository_scope(
            search_memory.ROOT
        ).as_dict(),
    }

    class Catalog:
        generations_path = catalog.generations_path

        def get_active_for_repository(self, _repository_scope, **_kwargs):
            return manifest

    results = search_memory.search(
        "stale vector needle",
        semantic=True,
        catalog=Catalog(),
        generation_embedder=lambda texts: np.array([[1.0, 0.0]], dtype=np.float32),
        generation_model_id="m",
        generation_model_revision="r1",
        graph=False,
        rerank=False,
        emit_telemetry=False,
    )
    assert results
    assert all(r["effective_mode"] == "BASE" for r in results)
    assert all(r["fallback_reason"] == "generation_vectors_unavailable" for r in results)


def test_lance_module_docs_match_ivf_pq_not_hnsw() -> None:
    import lance_store

    src = Path(lance_store.__file__).read_text(encoding="utf-8")
    assert "IVF_PQ" in src
    # Module docs must not claim HNSW is the selected default index.
    assert "vector IVF_PQ" in src or "IVF_PQ" in src.split("Architecture", 1)[-1]
    assert "HNSW" not in src.split('"""', 2)[1]
