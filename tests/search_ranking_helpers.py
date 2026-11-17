"""Helpers shared by test_search_ranking.py and the test files importing them."""
from __future__ import annotations

import pytest
from llm_wiki import runtime_roots


def _generation_snapshot(tmp_path, pages):
    from corpus_snapshot import collect_corpus

    vault = tmp_path / "vault"
    (vault / "knowledge/notes").mkdir(parents=True)
    for relative, content in pages.items():
        target = vault / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="")
    return vault, collect_corpus(vault)


def _activate_search_generation(tmp_path, snapshot, descriptors, *, vector=None):
    import generation_fts_validation
    from llm_wiki.generation_catalog_store import GenerationCatalog
    from reliable_memory import canonical_json_bytes
    from repository_scope import resolve_repository_scope

    catalog = GenerationCatalog(tmp_path / "state")
    generation_id = "gen-search"
    directory = catalog.generations_path / generation_id
    manifest = {
        "generation_id": generation_id,
        "schema_version": "corpus-generation/v1",
        "collector_version": snapshot.collector_version,
        "extractor_version": snapshot.extractor_version,
        "tokenizer_version": generation_fts_validation.GENERATION_TOKENIZER_VERSION,
        "tokenizer_config_sha256": generation_fts_validation.GENERATION_TOKENIZER_CONFIG_SHA256,
        "embedding_model_id": None,
        "embedding_model_revision": None,
        "vector_dimensions": None,
        "graph_schema_version": None,
        "graph_extractor_version": None,
        "source_manifest_sha256": snapshot.corpus_sha256,
        "artifacts": sorted(descriptors, key=lambda item: item["path"]),
        "vector_state": "absent",
        "repository_scope": resolve_repository_scope(runtime_roots.vault_root()).as_dict(),
    }
    if vector is not None:
        manifest.update(
            embedding_model_id=vector["model_id"],
            embedding_model_revision=vector["model_revision"],
            vector_dimensions=vector["dimensions"],
            vector_state="complete",
        )
    (directory / "manifest.json").write_bytes(canonical_json_bytes(manifest))
    catalog.register(generation_id)
    assert catalog.activate(generation_id, expected_active=None)
    return catalog, manifest


def _unregistered_vector_generation(tmp_path):
    import generation_fts_validation
    import generation_search
    import search_memory
    from repository_scope import resolve_repository_scope

    np = pytest.importorskip("numpy")
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
    descriptors = [generation_search.build_generation_fts(snapshot, generation)]
    descriptors.extend(
        search_memory.build_generation_numpy_vectors(
            snapshot,
            generation,
            embedder=lambda texts: np.eye(len(texts), 2, dtype=np.float32),
            model_id="deterministic/model",
            model_revision="revision-1",
            dimensions=2,
        )
    )
    manifest = {
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

    class Catalog:
        generations_path = generation_root

        def get_active_for_repository(self, _repository_scope, **_kwargs):
            return manifest

    return generation, manifest, Catalog()


def _refresh_artifact_descriptor(manifest, path):
    import generation_search

    descriptor = generation_search.artifact_descriptor(path, path.name)
    manifest["artifacts"] = [
        descriptor if item["path"] == path.name else item
        for item in manifest["artifacts"]
    ]
