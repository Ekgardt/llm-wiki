"""Opening the active generation must ask the identity question, not the commit.

NEW-111: on the live vault ``EvidenceGraph.open_active_for_repository`` raised
``PermissionError: active Evidence Graph changed while opening`` on every call.
Measured cause: the catalog admits the active generation by repository identity
(``same_repository``, the NEW-65 rule), but the open loop re-compared whole
scopes with ``!=`` — and on a vault that commits its own runtime the recorded
``git_commit`` (build-time provenance) almost never equals the checkout's
current commit, so every attempt hit ``continue`` and the retry budget ended in
``PermissionError``. These tests pin the identity rule at the open site without
weakening the torn-generation guarantee.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

COMMIT_BUILT = "a" * 40
COMMIT_NOW = "b" * 40


def _scope(tmp_path: Path, commit: str | None, name: str = "repo"):
    """A commit-bearing scope built from derived identities, no git needed."""
    # `str(Path)` yields backslashes on Windows, which the scope's canonical
    # drive-letter form refuses (CI run 33037811562, all five py versions on
    # shard s2). Serialize through the product's own canonicaliser instead.
    from repository_scope import (
        RepositoryScope,
        _local_serialized_path,
        derive_checkout_id,
        derive_repository_id,
    )

    root = _local_serialized_path(tmp_path / name, strict=False)
    common = _local_serialized_path(tmp_path / name / ".git", strict=False)
    repository_id = derive_repository_id(checkout_root=root, git_common_dir=common)
    return RepositoryScope(
        schema_version="repository-scope/v1",
        repository_id=repository_id,
        checkout_id=derive_checkout_id(repository_id, root),
        checkout_root=root,
        git_common_dir=common,
        git_commit=commit,
    )


def _publish_active(catalog, generation_id: str, repository_scope) -> None:
    import corpus_snapshot
    import evidence_graph
    from reliable_memory import canonical_json_bytes

    directory = catalog.generations_path / generation_id
    directory.mkdir(parents=True)
    content = b"def f(): pass\n"
    sources = [
        {
            "source_id": "source",
            "relative_path": "app.py",
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
            "media_type": "text/x-python",
            "language": "python",
            "git_oid": None,
        }
    ]
    policy = {
        "daily_paths": [],
        "code_roots": [],
        "include_historical": False,
        "as_of": None,
    }
    shared = [
        {"logical_id": "source", "relative_path": "app.py", "sha256": sources[0]["sha256"]}
    ]
    manifest_bytes = canonical_json_bytes(
        corpus_snapshot.canonical_source_manifest(shared, policy)
    )
    (directory / "source-manifest.json").write_bytes(manifest_bytes)
    database_path = directory / "evidence.sqlite3"
    evidence_graph.create_generation_database(
        database_path,
        sources=sources,
        source_bytes={"source": content},
        nodes=[],
        occurrences=[],
        assertions=[],
        evidence=[],
        observations=[],
        dependencies=[],
    )
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
        "source_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "artifacts": [
            {
                "path": "evidence.sqlite3",
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            },
            {
                "path": "source-manifest.json",
                "size": len(manifest_bytes),
                "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            },
        ],
        "vector_state": "absent",
        "repository_scope": repository_scope.as_dict(),
    }
    (directory / "manifest.json").write_bytes(canonical_json_bytes(manifest))
    catalog.register(generation_id)
    assert catalog.activate(generation_id, expected_active=None)


def _commit_moved_scopes(tmp_path):
    built_scope = _scope(tmp_path, COMMIT_BUILT)
    current_scope = _scope(tmp_path, COMMIT_NOW)
    assert built_scope.same_repository(current_scope)
    assert built_scope.git_commit != current_scope.git_commit
    return built_scope, current_scope


def _require_admitted_by_catalog(catalog, scope) -> None:
    manifest = catalog.get_active_for_repository(scope)
    assert manifest is not None
    assert manifest["generation_id"] == "gen-1"


def test_open_active_for_repository_opens_when_only_the_commit_moved(tmp_path):
    """The exact NEW-111 shape: same identity, different commit, catalog says yes.

    Before the fix the catalog returned the manifest (identity rule) while the
    open loop's whole-scope comparison hit ``continue`` three times and raised
    ``PermissionError: active Evidence Graph changed while opening``.
    """
    import evidence_graph
    from generation_catalog import GenerationCatalog

    built_scope, current_scope = _commit_moved_scopes(tmp_path)
    catalog = GenerationCatalog(tmp_path / "state")
    _publish_active(catalog, "gen-1", built_scope)
    # The catalog already admits this generation for the moved checkout;
    # the defect was that the open loop then contradicted it.
    _require_admitted_by_catalog(catalog, current_scope)

    graph = evidence_graph.EvidenceGraph.open_active_for_repository(
        catalog, current_scope
    )

    assert graph is not None
    assert graph.generation_id == "gen-1"
    # Provenance is preserved, not rewritten to the requested commit.
    assert graph.repository_scope.git_commit == COMMIT_BUILT
    graph.close()


def test_open_active_for_repository_still_refuses_a_torn_generation(tmp_path):
    """The commit-moved path must not weaken the torn-generation guarantee."""
    import evidence_graph
    from generation_catalog import GenerationCatalog

    catalog = GenerationCatalog(tmp_path / "state")
    _publish_active(catalog, "gen-1", _scope(tmp_path, COMMIT_BUILT))
    (catalog.generations_path / "gen-1" / "evidence.sqlite3").write_bytes(b"torn")

    graph = evidence_graph.EvidenceGraph.open_active_for_repository(
        catalog, _scope(tmp_path, COMMIT_NOW)
    )

    assert graph is None


def test_open_active_for_repository_still_refuses_another_checkout(tmp_path):
    """A different checkout identity stays ineligible even at the same commit."""
    import evidence_graph
    from generation_catalog import GenerationCatalog

    catalog = GenerationCatalog(tmp_path / "state")
    _publish_active(catalog, "gen-1", _scope(tmp_path, COMMIT_BUILT, name="one"))

    graph = evidence_graph.EvidenceGraph.open_active_for_repository(
        catalog, _scope(tmp_path, COMMIT_BUILT, name="two")
    )

    assert graph is None


def test_a_second_open_reuses_the_remembered_format_verdict(tmp_path, monkeypatch):
    """The constructor's closed-format pass runs once per exact artifact bytes."""
    import evidence_graph
    from generation_catalog import GenerationCatalog

    monkeypatch.setattr(evidence_graph, "_FORMAT_VALIDATED", set())
    passes = 0
    real_validate = evidence_graph._validate_connection

    def counting_validate(*args, **kwargs):
        nonlocal passes
        passes += 1
        return real_validate(*args, **kwargs)

    monkeypatch.setattr(evidence_graph, "_validate_connection", counting_validate)
    catalog = GenerationCatalog(tmp_path / "state")
    _publish_active(catalog, "gen-1", _scope(tmp_path, COMMIT_BUILT))
    after_publish = passes
    assert after_publish >= 1  # publication earned the verdict by running the pass

    first = evidence_graph.EvidenceGraph.open_active_for_repository(
        catalog, _scope(tmp_path, COMMIT_NOW)
    )
    first.close()
    second = evidence_graph.EvidenceGraph.open_active_for_repository(
        catalog, _scope(tmp_path, COMMIT_NOW)
    )
    second.close()

    assert first is not None
    assert second is not None
    assert passes == after_publish, "an open re-ran the closed-format pass"


def test_changed_artifact_bytes_do_not_inherit_the_remembered_verdict(tmp_path, monkeypatch):
    """A remembered verdict binds exact bytes; different bytes are validated."""
    import evidence_graph
    from generation_catalog import GenerationCatalog

    catalog = GenerationCatalog(tmp_path / "state")
    _publish_active(catalog, "gen-1", _scope(tmp_path, COMMIT_BUILT))
    monkeypatch.setattr(evidence_graph, "_FORMAT_VALIDATED", set())
    graph = evidence_graph.EvidenceGraph.open_active_for_repository(
        catalog, _scope(tmp_path, COMMIT_NOW)
    )
    graph.close()
    artifact = catalog.generations_path / "gen-1" / "evidence.sqlite3"
    artifact.write_bytes(b"torn bytes that hash differently")

    reopened = evidence_graph.EvidenceGraph.open_active_for_repository(
        catalog, _scope(tmp_path, COMMIT_NOW)
    )

    assert reopened is None
