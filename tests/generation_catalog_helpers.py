"""Helpers shared by test_generation_catalog.py and the test files importing them."""
from __future__ import annotations

import hashlib
import subprocess
from datetime import UTC, datetime
from pathlib import Path

NOW = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)


def _initialize_repository(repository: Path) -> None:
    subprocess.run(["git", "init", "--quiet", str(repository)], check=True, capture_output=True)


def _catalog(tmp_path: Path):
    from llm_wiki import generation_catalog_store

    state_root = tmp_path / "state"
    return generation_catalog_store.GenerationCatalog(state_root, clock=lambda: NOW)


def _publish(
    catalog,
    generation_id: str,
    *,
    parent: str | None = None,
    payload: bytes = b"search-index",
    vector_state: str = "absent",
    extra_artifacts: int = 0,
    repository_scope: dict[str, object] | None = None,
) -> tuple[Path, dict[str, object]]:
    from reliable_memory import canonical_json_bytes

    directory = catalog.generations_path / generation_id
    directory.mkdir(parents=True)
    artifact = directory / "search.sqlite3"
    artifact.write_bytes(payload)
    manifest: dict[str, object] = {
        "generation_id": generation_id,
        "schema_version": "corpus-generation/v1",
        "collector_version": "collector/v1",
        "extractor_version": "extractor/v1",
        "tokenizer_version": "tokenizer/v1",
        "tokenizer_config_sha256": hashlib.sha256(b"tokenizer-config").hexdigest(),
        "embedding_model_id": None,
        "embedding_model_revision": None,
        "vector_dimensions": None,
        "graph_schema_version": None,
        "graph_extractor_version": None,
        "source_manifest_sha256": hashlib.sha256(b"sources").hexdigest(),
        "artifacts": [
            {
                "path": "search.sqlite3",
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
        "vector_state": vector_state,
    }
    if parent is not None:
        manifest["parent_generation_id"] = parent
    if repository_scope is not None:
        manifest["repository_scope"] = repository_scope
    for number in range(extra_artifacts):
        name = f"artifact-{number:04d}.bin"
        content = f"artifact-{number:04d}".encode()
        (directory / name).write_bytes(content)
        manifest["artifacts"].append(
            {
                "path": name,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    manifest["artifacts"].sort(key=lambda artifact: artifact["path"])
    (directory / "manifest.json").write_bytes(canonical_json_bytes(manifest))
    return directory, manifest


def _scope_at_commit(tmp_path: Path, commit: str):
    """A Git-flavoured scope for a temporary checkout, at a chosen commit."""
    from repository_scope import (
        SCHEMA_VERSION,
        RepositoryScope,
        derive_checkout_id,
        derive_repository_id,
        resolve_repository_scope,
    )

    repository = tmp_path / "repository"
    repository.mkdir(exist_ok=True)
    _initialize_repository(repository)
    checkout_root = resolve_repository_scope(repository).checkout_root
    git_common_dir = f"{checkout_root}/.git"
    repository_id = derive_repository_id(
        checkout_root=checkout_root, git_common_dir=git_common_dir
    )
    return RepositoryScope(
        SCHEMA_VERSION,
        repository_id,
        derive_checkout_id(repository_id, checkout_root),
        checkout_root,
        git_common_dir,
        commit,
    )


def _publish_v2(catalog, generation_id: str) -> tuple[Path, dict[str, object]]:
    import corpus_snapshot
    import evidence_graph
    import generation_fts_validation
    import generation_search
    from reliable_memory import canonical_json_bytes
    from repository_scope import resolve_repository_scope

    vault = catalog.state_root.parent / f"vault-{generation_id}"
    (vault / "knowledge/notes").mkdir(parents=True)
    (vault / "knowledge/projects").mkdir(parents=True)
    _initialize_repository(vault)
    (vault / "knowledge/notes/page.md").write_text(
        "---\n"
        "type: concept\n"
        "project: generation-tests\n"
        "source_authority: user\n"
        "confidence: high\n"
        "status: active\n"
        "valid_from: 2026-01-01\n"
        "valid_to: 2027-01-01\n"
        "language: en\n"
        "---\n"
        "# Bound source\nunique generation content\n",
        encoding="utf-8",
    )
    snapshot = corpus_snapshot.collect_corpus(vault)
    directory = catalog.generations_path / generation_id
    directory.mkdir(parents=True)
    source_manifest = corpus_snapshot.canonical_source_manifest(
        (source.record for source in snapshot.sources), snapshot.policy
    )
    (directory / "source-manifest.json").write_bytes(canonical_json_bytes(source_manifest))
    evidence_graph.create_generation_database(
        directory / "evidence.sqlite3",
        sources=(
            {
                "source_id": source.record.logical_id,
                "relative_path": source.record.relative_path,
                "sha256": source.record.sha256,
                "size": source.record.size,
                "media_type": source.record.media_type,
                "language": source.record.language,
                "git_oid": source.record.git_oid,
            }
            for source in snapshot.sources
        ),
        source_bytes={source.record.logical_id: source.content for source in snapshot.sources},
        nodes=(),
        occurrences=(),
        assertions=(),
        evidence=(),
        observations=(),
        dependencies=(),
    )
    generation_search.build_generation_fts(snapshot, directory)

    artifacts = []
    for name in ("evidence.sqlite3", "search.sqlite3", "source-manifest.json"):
        payload = (directory / name).read_bytes()
        artifacts.append(
            {"path": name, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        )
    manifest = {
        "generation_id": generation_id,
        "schema_version": "corpus-generation/v2",
        "collector_version": snapshot.collector_version,
        "extractor_version": snapshot.extractor_version,
        "tokenizer_version": generation_fts_validation.GENERATION_TOKENIZER_VERSION,
        "tokenizer_config_sha256": generation_fts_validation.GENERATION_TOKENIZER_CONFIG_SHA256,
        "embedding_model_id": None,
        "embedding_model_revision": None,
        "vector_dimensions": None,
        "graph_schema_version": evidence_graph.GRAPH_SCHEMA_VERSION,
        "graph_extractor_version": "fixture-graph/v1",
        "source_manifest_sha256": snapshot.corpus_sha256,
        "artifacts": sorted(artifacts, key=lambda item: item["path"]),
        "vector_state": "absent",
        "repository_scope": resolve_repository_scope(vault).as_dict(),
    }
    (directory / "manifest.json").write_bytes(canonical_json_bytes(manifest))
    return directory, manifest
