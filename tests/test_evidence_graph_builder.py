"""Task 19: atomic full Evidence Graph generation builder.

The builder must:

- snapshot source membership and exact hashes before extraction,
- build an unpublished generation directory and database while readers
  continue using the prior generation,
- validate schema version, manifests, foreign keys, integrity_check,
  evidence spans, artifact hashes, and source membership,
- fsync files and directories where supported,
- activate by compare-and-swap against the expected active generation in
  one short catalog transaction,
- on startup ignore orphan/incomplete generations and fall back to the
  prior valid generation when the active target is missing or corrupt,
- never modify an active generation in place,
- support six named kill points so the contract above can be tested at
  every boundary: before_directory_create, during_extraction,
  after_database_commit, after_validation, before_activation,
  after_activation.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from tests.code_kernel_helpers import (
    basic_graph_records,
    build_fixture_generation,
    captured_snapshot_for_records,
    make_unminted_verified_subclass,
    snapshot_for_records,
)

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_builder_manifest_matches_database_schema_for_each_mode(tmp_path: Path) -> None:
    from evidence_graph import GraphSchema

    v2 = build_fixture_generation(
        tmp_path / "v2", generation_id="v2", graph_schema=GraphSchema.V2
    )
    v3 = build_fixture_generation(
        tmp_path / "v3", generation_id="v3", graph_schema=GraphSchema.V3
    )

    assert v2.manifest["graph_schema_version"] == "evidence-graph/v2"
    assert v3.manifest["graph_schema_version"] == "evidence-graph/v3"
    with __import__("sqlite3").connect(
        v3.generation_path / "evidence.sqlite3"
    ) as database:
        run_manifests = {
            row[0]
            for row in database.execute(
                "SELECT DISTINCT source_manifest_sha256 FROM analyzer_run"
            )
        }
    assert run_manifests == {v3.manifest["source_manifest_sha256"]}


def test_omitted_builder_schema_keeps_legacy_v2_manifest(tmp_path: Path) -> None:
    result = build_fixture_generation(tmp_path, generation_id="legacy")

    assert result.manifest["graph_schema_version"] == "evidence-graph/v2"
    with __import__("sqlite3").connect(
        result.generation_path / "evidence.sqlite3"
    ) as database:
        assert database.execute("PRAGMA user_version").fetchone()[0] == 2


def test_low_level_database_writer_has_no_code_capture_argument() -> None:
    import inspect

    import evidence_graph

    assert "code_capture" not in inspect.signature(
        evidence_graph.create_generation_database
    ).parameters


def test_v3_builder_rejects_unminted_verified_subclass_before_publication(
    tmp_path: Path,
) -> None:
    from evidence_graph import GraphSchema
    from evidence_graph_builder import build_full_generation
    from generation_catalog import GenerationCatalog
    from repository_scope import resolve_repository_scope

    records = basic_graph_records()
    repository = tmp_path / "repository"
    repository.mkdir()
    repository_scope = resolve_repository_scope(repository)
    catalog = GenerationCatalog(tmp_path / "state")
    snapshot = captured_snapshot_for_records(records)
    with pytest.raises(TypeError, match="VerifiedAnalysisBatch"):
        build_full_generation(
            catalog,
            generation_id="forged",
            graph_schema=GraphSchema.V3,
            verified_analyses=(make_unminted_verified_subclass(records),),
            snapshot=snapshot,
            code_capture=snapshot.code_capture,
            repository_scope=repository_scope,
            activate=False,
            **records,
        )
    assert not (catalog.generations_path / "forged").exists()
    assert catalog.get_active() is None


def test_v3_builder_requires_complete_repository_scoped_generation(tmp_path: Path) -> None:
    from evidence_graph import GraphSchema
    from evidence_graph_builder import build_full_generation
    from generation_catalog import GenerationCatalog

    with pytest.raises(ValueError, match="snapshot|repository scope|v3"):
        build_full_generation(
            GenerationCatalog(tmp_path / "state"),
            generation_id="invalid-v3",
            graph_schema=GraphSchema.V3,
            **basic_graph_records(),
        )


def test_v3_builder_requires_code_capture_before_creating_generation(tmp_path: Path) -> None:
    from evidence_graph import GraphSchema
    from evidence_graph_builder import build_full_generation
    from generation_catalog import GenerationCatalog
    from repository_scope import resolve_repository_scope

    records = basic_graph_records()
    repository = tmp_path / "repository"
    repository.mkdir()
    catalog = GenerationCatalog(tmp_path / "state")
    with pytest.raises(ValueError, match="code.capture"):
        build_full_generation(
            catalog,
            generation_id="no-capture",
            graph_schema=GraphSchema.V3,
            snapshot=snapshot_for_records(records),
            repository_scope=resolve_repository_scope(repository),
            activate=False,
            **records,
        )
    assert not (catalog.generations_path / "no-capture").exists()


def test_build_manifest_emits_exact_closed_code_capture_contract(tmp_path: Path) -> None:
    from code_workspace import code_capture_as_dict, collect_repository_code
    from evidence_graph import GraphSchema
    from evidence_graph_builder import _build_manifest

    repository = tmp_path / "repository"
    (repository / "src").mkdir(parents=True)
    (repository / "src/app.py").write_text("answer = 42\n", encoding="utf-8")
    snapshot = collect_repository_code(
        repository,
        roots=("src",),
        include_globs=("**/*.py",),
        ignore_globs=(),
        suffixes=(".py",),
    )
    manifest = _build_manifest(
        generation_id="capture",
        parent_generation_id=None,
        collector_version="collector/v1",
        extractor_version="extractor/v1",
        graph_extractor_version="graph/v1",
        source_manifest_sha256="1" * 64,
        database_size=1,
        database_sha256="2" * 64,
        source_manifest_bytes=b"{}",
        repository_scope=None,
        graph_schema=GraphSchema.V3,
        code_capture=snapshot.code_capture,
    )

    assert manifest["code_capture"] == code_capture_as_dict(snapshot.code_capture)
    assert set(manifest["code_capture"]) == {
        "policy",
        "limits",
        "files",
        "directories",
        "membership_sha256",
    }


def test_v3_builder_rejects_run_outside_manifest_repository_scope(tmp_path: Path) -> None:
    from code_intelligence import verify_native_analysis
    from evidence_graph import GraphSchema
    from evidence_graph_builder import build_full_generation
    from generation_catalog import GenerationCatalog
    from repository_scope import resolve_repository_scope

    from tests.code_kernel_helpers import (
        make_normalized_analysis_for_records,
        snapshot_for_records,
    )

    records = basic_graph_records()
    snapshot = snapshot_for_records(records)
    repository = tmp_path / "repository"
    repository.mkdir()
    with pytest.raises(ValueError, match="repository|checkout"):
        build_full_generation(
            GenerationCatalog(tmp_path / "state"),
            generation_id="wrong-scope",
            graph_schema=GraphSchema.V3,
            verified_analyses=(
                verify_native_analysis(snapshot, make_normalized_analysis_for_records(records)),
            ),
            snapshot=snapshot,
            repository_scope=resolve_repository_scope(repository),
            activate=False,
            **records,
        )


def _basic_records():
    content = b"def caller():\n    callee()\n"
    return {
        "sources": [
            {
                "source_id": "source",
                "relative_path": "app.py",
                "sha256": _sha(content),
                "size": len(content),
                "media_type": "text/x-python",
                "language": "python",
                "git_oid": None,
            }
        ],
        "source_bytes": {"source": content},
        "nodes": [
            {
                "node_id": "caller",
                "kind": "function",
                "identity_scheme": "python/v1",
                "identity_key": "app:caller",
                "metadata": {"name": "caller"},
            },
            {
                "node_id": "callee",
                "kind": "function",
                "identity_scheme": "python/v1",
                "identity_key": "app:callee",
                "metadata": {"name": "callee"},
            },
        ],
        "occurrences": [
            {
                "occurrence_id": "occurrence",
                "node_id": "caller",
                "source_id": "source",
                "role": "definition",
                "byte_start": 0,
                "byte_end": 12,
                "line_start": 1,
                "line_end": 1,
            }
        ],
        "assertions": [
            {
                "assertion_id": "assertion",
                "source_node_id": "caller",
                "edge_type": "CALLS",
                "target_node_id": "callee",
                "literal": None,
                "confidence": "high",
                "authority": "ai-derived",
                "resolution": "resolved",
                "extractor": "python/v1",
            }
        ],
        "evidence": [
            {
                "evidence_id": "evidence",
                "assertion_id": "assertion",
                "observation_id": None,
                "source_id": "source",
                "byte_start": 18,
                "byte_end": 26,
                "span_sha256": _sha(content[18:26]),
            }
        ],
        "observations": [
            {
                "observation_id": "observation",
                "source_node_id": "caller",
                "edge_type": "CALLS",
                "target_text": "dynamic",
                "reason": "dynamic_dispatch",
                "extractor": "python/v1",
            }
        ],
        "dependencies": [
            {
                "dependency_id": "dependency",
                "dependent_node_id": "caller",
                "dependency_node_id": "callee",
                "kind": "imports",
                "source_id": "source",
            }
        ],
    }


def _publish_baseline(catalog, generation_id: str = "gen-1") -> None:
    """Publish a minimal baseline generation and activate it."""
    import corpus_snapshot
    import evidence_graph
    from reliable_memory import canonical_json_bytes

    records = _basic_records()
    directory = catalog.generations_path / generation_id
    directory.mkdir(parents=True)
    policy = {
        "daily_paths": [],
        "code_roots": [],
        "include_historical": False,
        "as_of": None,
    }
    shared_sources = [
        {
            "logical_id": records["sources"][0]["source_id"],
            "relative_path": records["sources"][0]["relative_path"],
            "sha256": records["sources"][0]["sha256"],
        }
    ]
    source_manifest = corpus_snapshot.canonical_source_manifest(shared_sources, policy)
    source_manifest_bytes = canonical_json_bytes(source_manifest)
    (directory / "source-manifest.json").write_bytes(source_manifest_bytes)
    database_path = directory / "evidence.sqlite3"
    evidence_graph.create_generation_database(database_path, **records)
    payload = database_path.read_bytes()
    manifest = {
        "generation_id": generation_id,
        "schema_version": "corpus-generation/v1",
        "collector_version": corpus_snapshot.COLLECTOR_VERSION,
        "extractor_version": corpus_snapshot.EXTRACTOR_VERSION,
        "tokenizer_version": "tokenizer/v1",
        "tokenizer_config_sha256": hashlib.sha256(b"config").hexdigest(),
        "embedding_model_id": None,
        "embedding_model_revision": None,
        "vector_dimensions": None,
        "graph_schema_version": "evidence-graph/v2",
        "graph_extractor_version": "graph-extractor/v1",
        "source_manifest_sha256": hashlib.sha256(source_manifest_bytes).hexdigest(),
        "artifacts": [
            {
                "path": "evidence.sqlite3",
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            },
            {
                "path": "source-manifest.json",
                "size": len(source_manifest_bytes),
                "sha256": hashlib.sha256(source_manifest_bytes).hexdigest(),
            },
        ],
        "vector_state": "absent",
    }
    (directory / "manifest.json").write_bytes(canonical_json_bytes(manifest))
    catalog.register(generation_id)
    assert catalog.activate(generation_id, expected_active=None)


def _catalog(tmp_path):
    from generation_catalog import GenerationCatalog

    return GenerationCatalog(tmp_path / "state")


def test_complete_publication_semantically_validates_each_artifact_once(
    tmp_path, monkeypatch
):
    import corpus_snapshot
    import evidence_graph
    import search_memory
    from evidence_graph_builder import build_full_generation
    from repository_scope import resolve_repository_scope

    vault = tmp_path / "vault"
    (vault / "knowledge/notes").mkdir(parents=True)
    (vault / "knowledge/projects").mkdir(parents=True)
    (vault / "knowledge/notes/page.md").write_text(
        "---\ntype: concept\n---\n# Page\nBound content.\n", encoding="utf-8"
    )
    snapshot = corpus_snapshot.collect_corpus(vault)
    sources = [
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
    ]
    calls = {"evidence": 0, "fts": 0}
    real_evidence = evidence_graph.validate_generation_artifact
    real_fts = search_memory.validate_generation_fts_artifact

    def validate_evidence(*args, **kwargs):
        calls["evidence"] += 1
        return real_evidence(*args, **kwargs)

    def validate_fts(*args, **kwargs):
        calls["fts"] += 1
        return real_fts(*args, **kwargs)

    monkeypatch.setattr(evidence_graph, "validate_generation_artifact", validate_evidence)
    monkeypatch.setattr(search_memory, "validate_generation_fts_artifact", validate_fts)

    result = build_full_generation(
        _catalog(tmp_path),
        sources=sources,
        source_bytes={source.record.logical_id: source.content for source in snapshot.sources},
        nodes=(),
        occurrences=(),
        assertions=(),
        evidence=(),
        observations=(),
        dependencies=(),
        generation_id="one-validation",
        snapshot=snapshot,
        publication_root=vault,
        repository_scope=resolve_repository_scope(vault),
    )

    assert result.activated is True
    assert calls == {"evidence": 1, "fts": 1}


def test_activation_commit_close_failure_preserves_authoritative_generation(
    tmp_path, monkeypatch
):
    import sqlite3

    import corpus_snapshot
    import generation_catalog
    from evidence_graph_builder import build_full_generation
    from repository_scope import resolve_repository_scope

    vault = tmp_path / "vault"
    (vault / "knowledge/notes").mkdir(parents=True)
    (vault / "knowledge/projects").mkdir(parents=True)
    (vault / "knowledge/notes/page.md").write_text(
        "---\ntype: concept\n---\n# Page\nBound content.\n", encoding="utf-8"
    )
    snapshot = corpus_snapshot.collect_corpus(vault)
    sources = [
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
    ]
    catalog = _catalog(tmp_path)
    real_close = generation_catalog._GenerationSealCapability.close
    closes = 0

    def fail_after_activation_close(capability):
        nonlocal closes
        closes += 1
        real_close(capability)
        if closes == 2:
            raise OSError("activation descriptor close failure")

    monkeypatch.setattr(
        generation_catalog._GenerationSealCapability,
        "close",
        fail_after_activation_close,
    )

    with pytest.raises(OSError, match="activation descriptor close failure"):
        build_full_generation(
            catalog,
            sources=sources,
            source_bytes={source.record.logical_id: source.content for source in snapshot.sources},
            nodes=(),
            occurrences=(),
            assertions=(),
            evidence=(),
            observations=(),
            dependencies=(),
            generation_id="committed-close-failure",
            snapshot=snapshot,
            publication_root=vault,
            repository_scope=resolve_repository_scope(vault),
        )

    generation_path = catalog.generations_path / "committed-close-failure"
    with sqlite3.connect(catalog.catalog_path) as database:
        registered = database.execute(
            "SELECT COUNT(*) FROM generations WHERE generation_id = ?",
            ("committed-close-failure",),
        ).fetchone()[0]
        active = database.execute(
            "SELECT active_generation_id FROM catalog_state WHERE singleton = 1"
        ).fetchone()[0]
        historical = database.execute(
            "SELECT COUNT(*) FROM activation_history WHERE generation_id = ?",
            ("committed-close-failure",),
        ).fetchone()[0]
    assert (registered, active, historical) == (1, "committed-close-failure", 1)
    assert generation_path.is_dir()


def test_complete_publication_rejects_wrong_repository_scope_after_validation(
    tmp_path,
):
    import corpus_snapshot
    from evidence_graph_builder import build_full_generation
    from repository_scope import resolve_repository_scope

    vault = tmp_path / "vault"
    foreign = tmp_path / "foreign"
    for root in (vault, foreign):
        (root / "knowledge/notes").mkdir(parents=True)
        (root / "knowledge/projects").mkdir(parents=True)
    (vault / "knowledge/notes/page.md").write_text(
        "---\ntype: concept\n---\n# Page\nBound content.\n", encoding="utf-8"
    )
    snapshot = corpus_snapshot.collect_corpus(vault)
    sources = [
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
    ]
    catalog = _catalog(tmp_path)

    with pytest.raises(ValueError, match="publication root.*repository scope"):
        build_full_generation(
            catalog,
            sources=sources,
            source_bytes={
                source.record.logical_id: source.content for source in snapshot.sources
            },
            nodes=(),
            occurrences=(),
            assertions=(),
            evidence=(),
            observations=(),
            dependencies=(),
            generation_id="wrong-scope",
            snapshot=snapshot,
            publication_root=vault,
            repository_scope=resolve_repository_scope(foreign),
        )

    assert catalog.get_active() is None
    assert not (catalog.generations_path / "wrong-scope").exists()


def test_complete_publication_rechecks_live_sources_after_semantic_validation(
    tmp_path, monkeypatch
):
    import corpus_snapshot
    import search_memory
    from evidence_graph_builder import build_full_generation
    from repository_scope import resolve_repository_scope

    vault = tmp_path / "vault"
    (vault / "knowledge/notes").mkdir(parents=True)
    (vault / "knowledge/projects").mkdir(parents=True)
    page = vault / "knowledge/notes/page.md"
    page.write_text(
        "---\ntype: concept\n---\n# Page\nBound content.\n", encoding="utf-8"
    )
    snapshot = corpus_snapshot.collect_corpus(vault)
    sources = [
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
    ]
    catalog = _catalog(tmp_path)
    real_publish = search_memory._publish_validated_generation

    def mutate_then_publish(*args, **kwargs):
        page.write_text(
            "---\ntype: concept\n---\n# Page\nDrift content.\n", encoding="utf-8"
        )
        return real_publish(*args, **kwargs)

    monkeypatch.setattr(
        search_memory, "_publish_validated_generation", mutate_then_publish
    )

    with pytest.raises(corpus_snapshot.CorpusChanged):
        build_full_generation(
            catalog,
            sources=sources,
            source_bytes={
                source.record.logical_id: source.content for source in snapshot.sources
            },
            nodes=(),
            occurrences=(),
            assertions=(),
            evidence=(),
            observations=(),
            dependencies=(),
            generation_id="live-source-drift",
            snapshot=snapshot,
            publication_root=vault,
            repository_scope=resolve_repository_scope(vault),
        )

    assert catalog.get_active() is None
    assert not (catalog.generations_path / "live-source-drift").exists()


def test_build_manifest_emits_exact_canonical_repository_scope(tmp_path):
    from evidence_graph_builder import build_full_generation
    from repository_scope import RepositoryScope, resolve_repository_scope

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    scope = resolve_repository_scope(checkout)
    catalog = _catalog(tmp_path)
    records = _basic_records()

    result = build_full_generation(
        catalog,
        generation_id="scoped",
        sources=records["sources"],
        source_bytes=records["source_bytes"],
        nodes=records["nodes"],
        occurrences=records["occurrences"],
        assertions=records["assertions"],
        evidence=records["evidence"],
        observations=records["observations"],
        dependencies=records["dependencies"],
        repository_scope=scope,
    )

    on_disk = json.loads((result.generation_path / "manifest.json").read_bytes())
    assert on_disk["repository_scope"] == scope.as_dict()
    assert RepositoryScope.from_dict(on_disk["repository_scope"]) == scope


def test_build_creates_valid_generation_and_activates_under_cas(tmp_path):
    from evidence_graph_builder import BuildResult, build_full_generation

    catalog = _catalog(tmp_path)
    _publish_baseline(catalog)

    records = _basic_records()
    result = build_full_generation(
        catalog,
        generation_id="gen-2",
        parent_generation_id="gen-1",
        sources=records["sources"],
        source_bytes=records["source_bytes"],
        nodes=records["nodes"],
        occurrences=records["occurrences"],
        assertions=records["assertions"],
        evidence=records["evidence"],
        observations=records["observations"],
        dependencies=records["dependencies"],
        expected_active="gen-1",
    )

    assert isinstance(result, BuildResult)
    assert result.generation_id == "gen-2"
    assert result.activated is True
    active = catalog.get_active()
    assert active["generation_id"] == "gen-2"
    artifacts = {item["path"] for item in active["artifacts"]}
    assert artifacts == {"evidence.sqlite3", "source-manifest.json"}


def test_build_does_not_mutate_active_generation_until_activation(tmp_path):
    from evidence_graph_builder import build_full_generation

    catalog = _catalog(tmp_path)
    _publish_baseline(catalog)

    records = _basic_records()
    result = build_full_generation(
        catalog,
        generation_id="gen-2",
        parent_generation_id="gen-1",
        sources=records["sources"],
        source_bytes=records["source_bytes"],
        nodes=records["nodes"],
        occurrences=records["occurrences"],
        assertions=records["assertions"],
        evidence=records["evidence"],
        observations=records["observations"],
        dependencies=records["dependencies"],
        expected_active="gen-1",
        activate=False,
    )

    assert result.activated is False
    assert catalog.get_active()["generation_id"] == "gen-1"
    # gen-2 is registered and can be activated manually afterward.
    assert catalog.activate("gen-2", expected_active="gen-1")


def test_build_rejects_cas_mismatch_against_expected_active(tmp_path):
    from evidence_graph_builder import build_full_generation

    catalog = _catalog(tmp_path)
    _publish_baseline(catalog)

    records = _basic_records()
    # Caller claims the prior active is gen-9 (a lie). CAS must reject.
    result = build_full_generation(
        catalog,
        generation_id="gen-2",
        parent_generation_id="gen-1",
        sources=records["sources"],
        source_bytes=records["source_bytes"],
        nodes=records["nodes"],
        occurrences=records["occurrences"],
        assertions=records["assertions"],
        evidence=records["evidence"],
        observations=records["observations"],
        dependencies=records["dependencies"],
        expected_active="gen-9",
    )

    assert result.activated is False
    assert catalog.get_active()["generation_id"] == "gen-1"


def test_build_rejects_invalid_evidence_records(tmp_path):
    from evidence_graph_builder import build_full_generation

    catalog = _catalog(tmp_path)
    _publish_baseline(catalog)

    records = _basic_records()
    records["evidence"][0]["span_sha256"] = "0" * 64

    with pytest.raises(ValueError, match="hash|span|evidence"):
        build_full_generation(
            catalog,
            generation_id="gen-2",
            parent_generation_id="gen-1",
            sources=records["sources"],
            source_bytes=records["source_bytes"],
            nodes=records["nodes"],
            occurrences=records["occurrences"],
            assertions=records["assertions"],
            evidence=records["evidence"],
            observations=records["observations"],
            dependencies=records["dependencies"],
            expected_active="gen-1",
        )
    # No partial generation directory left behind on validation failure.
    assert not (catalog.generations_path / "gen-2").exists()
    assert catalog.get_active()["generation_id"] == "gen-1"


def test_build_refuses_to_overwrite_an_existing_generation_directory(tmp_path):
    from evidence_graph_builder import build_full_generation

    catalog = _catalog(tmp_path)
    _publish_baseline(catalog)

    records = _basic_records()
    build_full_generation(
        catalog,
        generation_id="gen-2",
        parent_generation_id="gen-1",
        sources=records["sources"],
        source_bytes=records["source_bytes"],
        nodes=records["nodes"],
        occurrences=records["occurrences"],
        assertions=records["assertions"],
        evidence=records["evidence"],
        observations=records["observations"],
        dependencies=records["dependencies"],
        expected_active="gen-1",
    )

    with pytest.raises((FileExistsError, ValueError)):
        build_full_generation(
            catalog,
            generation_id="gen-2",
            parent_generation_id="gen-1",
            sources=records["sources"],
            source_bytes=records["source_bytes"],
            nodes=records["nodes"],
            occurrences=records["occurrences"],
            assertions=records["assertions"],
            evidence=records["evidence"],
            observations=records["observations"],
            dependencies=records["dependencies"],
            expected_active="gen-2",
        )


def test_build_writes_manifest_with_source_manifest_sha256_and_artifact_hashes(tmp_path):
    import corpus_snapshot
    from evidence_graph_builder import build_full_generation
    from reliable_memory import canonical_json_bytes

    catalog = _catalog(tmp_path)
    _publish_baseline(catalog)

    records = _basic_records()
    result = build_full_generation(
        catalog,
        generation_id="gen-2",
        parent_generation_id="gen-1",
        sources=records["sources"],
        source_bytes=records["source_bytes"],
        nodes=records["nodes"],
        occurrences=records["occurrences"],
        assertions=records["assertions"],
        evidence=records["evidence"],
        observations=records["observations"],
        dependencies=records["dependencies"],
        expected_active="gen-1",
    )

    manifest_path = result.generation_path / "manifest.json"
    source_manifest_path = result.generation_path / "source-manifest.json"
    assert manifest_path.exists()
    assert source_manifest_path.exists()
    manifest = json.loads(manifest_path.read_bytes())
    source_manifest_bytes = source_manifest_path.read_bytes()
    # Manifest must be canonical JSON and bind exact artifact hashes.
    assert canonical_json_bytes(manifest) == manifest_path.read_bytes()
    assert manifest["source_manifest_sha256"] == hashlib.sha256(source_manifest_bytes).hexdigest()
    # Every artifact descriptor matches the bytes on disk.
    by_path = {item["path"]: item for item in manifest["artifacts"]}
    for path, descriptor in by_path.items():
        actual = (result.generation_path / path).read_bytes()
        assert descriptor["size"] == len(actual)
        assert descriptor["sha256"] == hashlib.sha256(actual).hexdigest()
    # Source manifest matches the canonical snapshot of the supplied sources.
    shared_sources = [
        {
            "logical_id": records["sources"][0]["source_id"],
            "relative_path": records["sources"][0]["relative_path"],
            "sha256": records["sources"][0]["sha256"],
        }
    ]
    policy = {
        "daily_paths": [],
        "code_roots": [],
        "include_historical": False,
        "as_of": None,
    }
    expected_manifest = corpus_snapshot.canonical_source_manifest(shared_sources, policy)
    assert json.loads(source_manifest_bytes) == expected_manifest


@pytest.mark.parametrize(
    "kill_point",
    [
        "before_directory_create",
        "during_extraction",
        "after_database_commit",
        "after_validation",
        "before_activation",
        "after_activation",
    ],
)
def test_six_kill_points_leave_prior_generation_reachable(tmp_path, kill_point):
    """Each kill point must leave the prior active generation fully readable."""
    from evidence_graph import EvidenceGraph
    from evidence_graph_builder import KillPointError, build_full_generation

    catalog = _catalog(tmp_path)
    _publish_baseline(catalog)

    records = _basic_records()
    with pytest.raises(KillPointError) as exc_info:
        build_full_generation(
            catalog,
            generation_id="gen-2",
            parent_generation_id="gen-1",
            sources=records["sources"],
            source_bytes=records["source_bytes"],
            nodes=records["nodes"],
            occurrences=records["occurrences"],
            assertions=records["assertions"],
            evidence=records["evidence"],
            observations=records["observations"],
            dependencies=records["dependencies"],
            expected_active="gen-1",
            kill_point=kill_point,
        )
    assert exc_info.value.kill_point == kill_point

    # The active pointer only advances when activation actually completes.
    active = catalog.get_active()
    if kill_point == "after_activation":
        assert active["generation_id"] == "gen-2"
    else:
        assert active["generation_id"] == "gen-1"

    # The catalog still opens a valid read facade against whatever is active.
    graph = EvidenceGraph.open_active(catalog)
    assert graph is not None
    assert graph.generation_id == active["generation_id"]
    graph.close()


def test_kill_point_before_activation_leaves_generation_registered(tmp_path):
    """A pre-activation kill still records the generation so callers can retry."""
    from evidence_graph_builder import KillPointError, build_full_generation

    catalog = _catalog(tmp_path)
    _publish_baseline(catalog)

    records = _basic_records()
    with pytest.raises(KillPointError):
        build_full_generation(
            catalog,
            generation_id="gen-2",
            parent_generation_id="gen-1",
            sources=records["sources"],
            source_bytes=records["source_bytes"],
            nodes=records["nodes"],
            occurrences=records["occurrences"],
            assertions=records["assertions"],
            evidence=records["evidence"],
            observations=records["observations"],
            dependencies=records["dependencies"],
            expected_active="gen-1",
            kill_point="before_activation",
        )

    assert catalog.get_active()["generation_id"] == "gen-1"
    # gen-2 is registered; a caller-driven CAS activation should succeed.
    assert catalog.activate("gen-2", expected_active="gen-1")


def test_orphan_directory_from_during_extraction_is_ignored_after_restart(tmp_path):
    """A crash mid-extraction leaves an orphan directory; a new catalog process
    must ignore it and continue serving the prior active generation."""
    from evidence_graph_builder import KillPointError, build_full_generation
    from generation_catalog import GenerationCatalog

    catalog = _catalog(tmp_path)
    _publish_baseline(catalog)

    records = _basic_records()
    with pytest.raises(KillPointError):
        build_full_generation(
            catalog,
            generation_id="gen-2",
            parent_generation_id="gen-1",
            sources=records["sources"],
            source_bytes=records["source_bytes"],
            nodes=records["nodes"],
            occurrences=records["occurrences"],
            assertions=records["assertions"],
            evidence=records["evidence"],
            observations=records["observations"],
            dependencies=records["dependencies"],
            expected_active="gen-1",
            kill_point="during_extraction",
        )

    # The orphan directory exists on disk.
    assert (catalog.generations_path / "gen-2").exists()
    # A new catalog instance (simulating a restart) skips the orphan and
    # falls back to gen-1 as the active generation.
    restarted = GenerationCatalog(catalog.state_root)
    active = restarted.get_active()
    assert active["generation_id"] == "gen-1"


def test_orphan_directory_from_after_database_commit_is_ignored_after_restart(tmp_path):
    """A crash after the database commit but before manifest write must still
    leave the prior generation reachable."""
    from evidence_graph_builder import KillPointError, build_full_generation
    from generation_catalog import GenerationCatalog

    catalog = _catalog(tmp_path)
    _publish_baseline(catalog)

    records = _basic_records()
    with pytest.raises(KillPointError):
        build_full_generation(
            catalog,
            generation_id="gen-2",
            parent_generation_id="gen-1",
            sources=records["sources"],
            source_bytes=records["source_bytes"],
            nodes=records["nodes"],
            occurrences=records["occurrences"],
            assertions=records["assertions"],
            evidence=records["evidence"],
            observations=records["observations"],
            dependencies=records["dependencies"],
            expected_active="gen-1",
            kill_point="after_database_commit",
        )

    restarted = GenerationCatalog(catalog.state_root)
    active = restarted.get_active()
    assert active["generation_id"] == "gen-1"


def test_builder_never_mutates_active_generation_in_place(tmp_path):
    """Building a second generation must not touch the bytes of the active one."""
    from evidence_graph_builder import build_full_generation

    catalog = _catalog(tmp_path)
    _publish_baseline(catalog)

    active_before = (catalog.generations_path / "gen-1" / "evidence.sqlite3").stat().st_mtime_ns

    records = _basic_records()
    build_full_generation(
        catalog,
        generation_id="gen-2",
        parent_generation_id="gen-1",
        sources=records["sources"],
        source_bytes=records["source_bytes"],
        nodes=records["nodes"],
        occurrences=records["occurrences"],
        assertions=records["assertions"],
        evidence=records["evidence"],
        observations=records["observations"],
        dependencies=records["dependencies"],
        expected_active="gen-1",
    )

    active_after = (catalog.generations_path / "gen-1" / "evidence.sqlite3").stat().st_mtime_ns
    assert active_before == active_after


def test_builder_readers_continue_using_prior_generation_during_build(tmp_path):
    """An open Evidence Graph facade over gen-1 must keep working while the
    next generation is being built."""
    from evidence_graph import EvidenceGraph
    from evidence_graph_builder import build_full_generation

    catalog = _catalog(tmp_path)
    _publish_baseline(catalog)
    graph = EvidenceGraph.open_active(catalog)
    assert graph.generation_id == "gen-1"

    records = _basic_records()
    build_full_generation(
        catalog,
        generation_id="gen-2",
        parent_generation_id="gen-1",
        sources=records["sources"],
        source_bytes=records["source_bytes"],
        nodes=records["nodes"],
        occurrences=records["occurrences"],
        assertions=records["assertions"],
        evidence=records["evidence"],
        observations=records["observations"],
        dependencies=records["dependencies"],
        expected_active="gen-1",
    )

    # The pre-existing reader still sees gen-1 until it reopens.
    assert graph.generation_id == "gen-1"
    graph.close()
    # After reopen, the reader sees the newly active generation.
    reopened = EvidenceGraph.open_active(catalog)
    assert reopened.generation_id == "gen-2"
    reopened.close()


def test_builder_rejects_unknown_kill_point(tmp_path):
    from evidence_graph_builder import build_full_generation

    catalog = _catalog(tmp_path)
    _publish_baseline(catalog)

    records = _basic_records()
    with pytest.raises(ValueError, match="kill_point"):
        build_full_generation(
            catalog,
            generation_id="gen-2",
            parent_generation_id="gen-1",
            sources=records["sources"],
            source_bytes=records["source_bytes"],
            nodes=records["nodes"],
            occurrences=records["occurrences"],
            assertions=records["assertions"],
            evidence=records["evidence"],
            observations=records["observations"],
            dependencies=records["dependencies"],
            expected_active="gen-1",
            kill_point="not-a-real-kill-point",
        )


def test_builder_snapshot_source_hash_is_pinned_in_manifest(tmp_path):
    """The source manifest SHA-256 is captured before extraction and must
    match the manifest recorded on disk."""
    import corpus_snapshot
    from evidence_graph_builder import build_full_generation

    catalog = _catalog(tmp_path)
    _publish_baseline(catalog)

    records = _basic_records()
    shared_sources = [
        {
            "logical_id": records["sources"][0]["source_id"],
            "relative_path": records["sources"][0]["relative_path"],
            "sha256": records["sources"][0]["sha256"],
        }
    ]
    policy = {
        "daily_paths": [],
        "code_roots": [],
        "include_historical": False,
        "as_of": None,
    }
    expected_hash = corpus_snapshot.canonical_source_manifest_sha256(shared_sources, policy)

    result = build_full_generation(
        catalog,
        generation_id="gen-2",
        parent_generation_id="gen-1",
        sources=records["sources"],
        source_bytes=records["source_bytes"],
        nodes=records["nodes"],
        occurrences=records["occurrences"],
        assertions=records["assertions"],
        evidence=records["evidence"],
        observations=records["observations"],
        dependencies=records["dependencies"],
        expected_active="gen-1",
    )

    manifest = json.loads((result.generation_path / "manifest.json").read_bytes())
    assert manifest["source_manifest_sha256"] == expected_hash


def test_builder_runs_full_schema_fk_integrity_and_evidence_validation(tmp_path):
    """The post-commit validation pass must include schema version, FKs,
    integrity_check, evidence spans, and source membership."""
    from evidence_graph_builder import build_full_generation
    from reliable_memory import canonical_json_bytes

    catalog = _catalog(tmp_path)
    _publish_baseline(catalog)

    records = _basic_records()
    result = build_full_generation(
        catalog,
        generation_id="gen-2",
        parent_generation_id="gen-1",
        sources=records["sources"],
        source_bytes=records["source_bytes"],
        nodes=records["nodes"],
        occurrences=records["occurrences"],
        assertions=records["assertions"],
        evidence=records["evidence"],
        observations=records["observations"],
        dependencies=records["dependencies"],
        expected_active="gen-1",
    )

    # The manifest must validate against the committed closed schema and
    # the catalog must agree that the generation is intact.
    manifest_bytes = (result.generation_path / "manifest.json").read_bytes()
    assert canonical_json_bytes(json.loads(manifest_bytes)) == manifest_bytes
    # catalog.register already re-validates; reaching get_active() proves the
    # full validation pipeline (schema, FK, integrity_check, evidence, source
    # membership, artifact hashes) succeeded.
    active = catalog.get_active()
    assert active["generation_id"] == "gen-2"


def test_kill_point_after_validation_leaves_generation_artifacts_on_disk(tmp_path):
    """``after_validation`` fires after artifact validation but before catalog
    registration: the directory exists, the prior generation is still active,
    and a caller-driven ``catalog.register`` completes the publish."""
    from evidence_graph_builder import KillPointError, build_full_generation

    catalog = _catalog(tmp_path)
    _publish_baseline(catalog)

    records = _basic_records()
    with pytest.raises(KillPointError):
        build_full_generation(
            catalog,
            generation_id="gen-2",
            parent_generation_id="gen-1",
            sources=records["sources"],
            source_bytes=records["source_bytes"],
            nodes=records["nodes"],
            occurrences=records["occurrences"],
            assertions=records["assertions"],
            evidence=records["evidence"],
            observations=records["observations"],
            dependencies=records["dependencies"],
            expected_active="gen-1",
            kill_point="after_validation",
        )

    # Active pointer unchanged.
    assert catalog.get_active()["generation_id"] == "gen-1"
    # Validated artifacts are on disk; a caller-driven register + activate
    # completes the publish without re-running extraction.
    assert (catalog.generations_path / "gen-2" / "manifest.json").exists()
    catalog.register("gen-2")
    assert catalog.activate("gen-2", expected_active="gen-1")


def test_builder_snapshots_and_hashes_sources_before_consuming_extractor_iterables(tmp_path):
    from evidence_graph_builder import build_full_generation

    catalog = _catalog(tmp_path)
    records = _basic_records()
    state = {"sources_complete": False, "extractor_consumed": False}

    def lazy_sources():
        yield records["sources"][0]
        state["sources_complete"] = True

    def lazy_nodes():
        state["extractor_consumed"] = True
        assert state["sources_complete"]
        yield from records["nodes"]

    build_full_generation(
        catalog,
        generation_id="gen-1",
        sources=lazy_sources(),
        source_bytes=records["source_bytes"],
        nodes=lazy_nodes(),
        occurrences=records["occurrences"],
        assertions=records["assertions"],
        evidence=records["evidence"],
        observations=records["observations"],
        dependencies=records["dependencies"],
        expected_active=None,
    )

    assert state == {"sources_complete": True, "extractor_consumed": True}


def test_builder_rejects_source_hash_before_consuming_extractor_iterables(tmp_path):
    from evidence_graph_builder import build_full_generation

    catalog = _catalog(tmp_path)
    records = _basic_records()
    records["sources"][0]["sha256"] = "0" * 64

    def forbidden_nodes():
        pytest.fail("extractor iterable consumed before source snapshot verification")
        yield

    with pytest.raises(ValueError, match="source.*hash|hash.*source"):
        build_full_generation(
            catalog,
            generation_id="gen-1",
            sources=iter(records["sources"]),
            source_bytes=records["source_bytes"],
            nodes=forbidden_nodes(),
            occurrences=records["occurrences"],
            assertions=records["assertions"],
            evidence=records["evidence"],
            observations=records["observations"],
            dependencies=records["dependencies"],
            expected_active=None,
        )


def test_builder_source_snapshot_is_immutable_while_lazy_extractor_runs(tmp_path):
    import sqlite3

    from evidence_graph_builder import build_full_generation

    catalog = _catalog(tmp_path)
    records = _basic_records()

    def mutating_nodes():
        records["sources"][0]["relative_path"] = "changed.py"
        records["source_bytes"]["source"] = b"changed"
        yield from records["nodes"]

    result = build_full_generation(
        catalog,
        generation_id="gen-1",
        sources=records["sources"],
        source_bytes=records["source_bytes"],
        nodes=mutating_nodes(),
        occurrences=records["occurrences"],
        assertions=records["assertions"],
        evidence=records["evidence"],
        observations=records["observations"],
        dependencies=records["dependencies"],
        expected_active=None,
    )

    with sqlite3.connect(result.generation_path / "evidence.sqlite3") as database:
        relative_path, content = database.execute(
            "SELECT relative_path, content FROM source"
        ).fetchone()
    assert relative_path == "app.py"
    assert content == b"def caller():\n    callee()\n"


def test_builder_cancellation_stops_lazy_iterable_materialization_before_disk_write(tmp_path):
    from evidence_graph_builder import build_full_generation

    catalog = _catalog(tmp_path)
    records = _basic_records()
    checks = 0

    def cancelled():
        nonlocal checks
        checks += 1
        return checks >= 4

    with pytest.raises(TimeoutError, match="cancel"):
        build_full_generation(
            catalog,
            generation_id="gen-1",
            sources=iter(records["sources"]),
            source_bytes=records["source_bytes"],
            nodes=iter(records["nodes"]),
            occurrences=records["occurrences"],
            assertions=records["assertions"],
            evidence=records["evidence"],
            observations=records["observations"],
            dependencies=records["dependencies"],
            expected_active=None,
            cancelled=cancelled,
        )

    assert not (catalog.generations_path / "gen-1").exists()


@pytest.mark.parametrize(
    "field",
    [
        "sources",
        "nodes",
        "occurrences",
        "assertions",
        "evidence",
        "observations",
        "dependencies",
    ],
)
def test_builder_rejects_generator_overflow_before_generation_create(
    tmp_path, monkeypatch, field
):
    import evidence_graph
    from evidence_graph_builder import build_full_generation

    catalog = _catalog(tmp_path)
    records = _basic_records()
    monkeypatch.setattr(evidence_graph, "MAX_VALIDATION_ROWS", 2)
    records[field] = ({} for _ in range(3))

    with pytest.raises(ValueError, match=f"{field}.*ceiling"):
        build_full_generation(
            catalog,
            generation_id="gen-overflow",
            expected_active=None,
            **records,
        )

    assert not (catalog.generations_path / "gen-overflow").exists()


def test_repeated_cas_losers_are_unregistered_and_removed_durably(tmp_path):
    import sqlite3

    from evidence_graph_builder import build_full_generation

    catalog = _catalog(tmp_path)
    records = _basic_records()

    for generation_id in ("cas-loser-1", "cas-loser-2"):
        result = build_full_generation(
            catalog,
            generation_id=generation_id,
            expected_active="stale-active",
            **records,
        )
        assert result.activated is False
        assert not result.generation_path.exists()

    with sqlite3.connect(catalog.catalog_path) as database:
        assert database.execute("SELECT COUNT(*) FROM generations").fetchone()[0] == 0
    assert catalog.recover_orphans() == []


def test_builder_propagates_real_file_fsync_failure(tmp_path, monkeypatch):
    import evidence_graph_builder

    catalog = _catalog(tmp_path)
    records = _basic_records()
    real_fsync = evidence_graph_builder.fsync_file

    def fail_fsync(path):
        if Path(path).name == "source-manifest.json":
            raise OSError("durability failure")
        return real_fsync(path)

    monkeypatch.setattr(evidence_graph_builder, "fsync_file", fail_fsync)

    with pytest.raises(OSError, match="durability failure"):
        evidence_graph_builder.build_full_generation(
            catalog,
            generation_id="gen-1",
            sources=records["sources"],
            source_bytes=records["source_bytes"],
            nodes=records["nodes"],
            occurrences=records["occurrences"],
            assertions=records["assertions"],
            evidence=records["evidence"],
            observations=records["observations"],
            dependencies=records["dependencies"],
            expected_active=None,
        )


def test_builder_deadline_stops_lazy_materialization_before_generation_create(
    tmp_path, monkeypatch
):
    import evidence_graph_builder

    catalog = _catalog(tmp_path)
    records = _basic_records()
    ticks = iter((0.0, 0.0, 2.0))
    monkeypatch.setattr(evidence_graph_builder.time, "monotonic", lambda: next(ticks, 2.0))

    with pytest.raises(TimeoutError, match="deadline"):
        evidence_graph_builder.build_full_generation(
            catalog,
            generation_id="gen-1",
            sources=iter(records["sources"]),
            source_bytes=records["source_bytes"],
            nodes=iter(records["nodes"]),
            occurrences=records["occurrences"],
            assertions=records["assertions"],
            evidence=records["evidence"],
            observations=records["observations"],
            dependencies=records["dependencies"],
            expected_active=None,
            deadline=1.0,
        )

    assert not (catalog.generations_path / "gen-1").exists()


def test_builder_cancellation_after_directory_create_cleans_unpublished_generation(
    tmp_path, monkeypatch
):
    import evidence_graph_builder

    catalog = _catalog(tmp_path)
    records = _basic_records()
    cancelled = False
    real_create = evidence_graph_builder.evidence_graph.create_generation_database

    def cancel_during_database(*args, **kwargs):
        nonlocal cancelled
        cancelled = True
        return real_create(*args, **kwargs)

    monkeypatch.setattr(
        evidence_graph_builder.evidence_graph,
        "create_generation_database",
        cancel_during_database,
    )

    with pytest.raises(TimeoutError, match="cancel"):
        evidence_graph_builder.build_full_generation(
            catalog,
            generation_id="gen-1",
            sources=records["sources"],
            source_bytes=records["source_bytes"],
            nodes=records["nodes"],
            occurrences=records["occurrences"],
            assertions=records["assertions"],
            evidence=records["evidence"],
            observations=records["observations"],
            dependencies=records["dependencies"],
            expected_active=None,
            cancelled=lambda: cancelled,
        )

    assert catalog.get_active() is None
    assert not (catalog.generations_path / "gen-1").exists()
