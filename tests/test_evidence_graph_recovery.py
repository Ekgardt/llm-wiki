"""Evidence Graph catalog reuse and immutable-generation recovery tests."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _publish(
    catalog,
    generation_id: str,
    *,
    parent: str | None = None,
    sources=None,
    source_bytes=None,
    graph_records=None,
):
    import corpus_snapshot
    import evidence_graph
    from reliable_memory import canonical_json_bytes

    directory = catalog.generations_path / generation_id
    directory.mkdir(parents=True)
    content = b"def f(): pass\n"
    if graph_records is not None:
        sources = graph_records["sources"]
        source_bytes = graph_records["source_bytes"]
    if sources is None:
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
    if source_bytes is None:
        source_bytes = {"source": content}
    policy = {
        "daily_paths": [],
        "code_roots": [],
        "include_historical": False,
        "as_of": None,
    }
    shared_sources = [
        {
            "logical_id": source["source_id"],
            "relative_path": source["relative_path"],
            "sha256": source["sha256"],
        }
        for source in sources
    ]
    source_manifest = corpus_snapshot.canonical_source_manifest(shared_sources, policy)
    source_manifest_bytes = canonical_json_bytes(source_manifest)
    (directory / "source-manifest.json").write_bytes(source_manifest_bytes)
    database_path = directory / "evidence.sqlite3"
    evidence_graph.create_generation_database(
        database_path,
        sources=sources,
        source_bytes=source_bytes,
        nodes=[] if graph_records is None else graph_records["nodes"],
        occurrences=[] if graph_records is None else graph_records["occurrences"],
        assertions=[] if graph_records is None else graph_records["assertions"],
        evidence=[] if graph_records is None else graph_records["evidence"],
        observations=[] if graph_records is None else graph_records["observations"],
        dependencies=[] if graph_records is None else graph_records["dependencies"],
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
        "graph_schema_version": "evidence-graph/v1",
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
    if parent is not None:
        manifest["parent_generation_id"] = parent
    (directory / "manifest.json").write_bytes(canonical_json_bytes(manifest))
    return manifest


def _rich_graph_records():
    content = b"def caller():\n    callee()\n"
    return {
        "sources": [
            {
                "source_id": "source",
                "relative_path": "app.py",
                "sha256": hashlib.sha256(content).hexdigest(),
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
                "span_sha256": hashlib.sha256(content[18:26]).hexdigest(),
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


def test_open_active_reuses_generation_catalog_and_falls_back_from_corruption(tmp_path):
    import evidence_graph
    from generation_catalog import GenerationCatalog

    catalog = GenerationCatalog(tmp_path / "state")
    _publish(catalog, "gen-1")
    _publish(catalog, "gen-2", parent="gen-1")
    for identifier in ("gen-1", "gen-2"):
        catalog.register(identifier)
    assert catalog.activate("gen-1", expected_active=None)
    assert catalog.activate("gen-2", expected_active="gen-1")
    (catalog.generations_path / "gen-2/evidence.sqlite3").write_bytes(b"corrupt")

    graph = evidence_graph.EvidenceGraph.open_active(catalog)

    assert graph is not None
    assert graph.generation_id == "gen-1"
    assert catalog.get_active()["generation_id"] == "gen-1"
    graph.close()


def test_open_active_returns_none_without_pointer_and_rejects_wrong_graph_contract(tmp_path):
    import evidence_graph
    from generation_catalog import GenerationCatalog

    catalog = GenerationCatalog(tmp_path / "state")
    assert evidence_graph.EvidenceGraph.open_active(catalog) is None

    manifest_value = _publish(catalog, "gen-1")
    manifest_value["graph_schema_version"] = "other-graph/v1"
    from reliable_memory import canonical_json_bytes

    manifest = catalog.generations_path / "gen-1/manifest.json"
    manifest.write_bytes(canonical_json_bytes(manifest_value))
    catalog.register("gen-1")
    assert catalog.activate("gen-1", expected_active=None)
    with pytest.raises(ValueError, match="Graph|graph"):
        evidence_graph.EvidenceGraph.open_active(catalog)


def _rebind_registered_manifest(catalog, generation_id: str) -> None:
    from reliable_memory import canonical_json_bytes

    directory = catalog.generations_path / generation_id
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    artifact = directory / "evidence.sqlite3"
    payload = artifact.read_bytes()
    graph_artifact = next(
        item for item in manifest["artifacts"] if item["path"] == "evidence.sqlite3"
    )
    graph_artifact.update(size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
    encoded = canonical_json_bytes(manifest)
    manifest_path.write_bytes(encoded)
    with sqlite3.connect(catalog.catalog_path) as database:
        database.execute(
            "UPDATE generations SET manifest_json=?, manifest_sha256=? WHERE generation_id=?",
            (encoded, hashlib.sha256(encoded).hexdigest(), generation_id),
        )


def test_catalog_skips_hash_valid_graph_with_wrong_schema_and_repairs_to_prior(tmp_path):
    import evidence_graph
    from generation_catalog import GenerationCatalog

    catalog = GenerationCatalog(tmp_path / "state")
    _publish(catalog, "gen-1")
    _publish(catalog, "gen-2", parent="gen-1")
    for identifier in ("gen-1", "gen-2"):
        catalog.register(identifier)
    assert catalog.activate("gen-1", expected_active=None)
    assert catalog.activate("gen-2", expected_active="gen-1")
    with sqlite3.connect(catalog.generations_path / "gen-2/evidence.sqlite3") as database:
        database.execute("DROP INDEX assertion_reverse")
    _rebind_registered_manifest(catalog, "gen-2")

    graph = evidence_graph.EvidenceGraph.open_active(catalog)

    assert graph is not None
    assert graph.generation_id == "gen-1"
    assert catalog.get_active()["generation_id"] == "gen-1"
    graph.close()


def test_registration_and_orphan_recovery_reject_malformed_hash_valid_graph(tmp_path):
    from generation_catalog import GenerationCatalog

    catalog = GenerationCatalog(tmp_path / "state")
    _publish(catalog, "malformed")
    with sqlite3.connect(catalog.generations_path / "malformed/evidence.sqlite3") as database:
        database.execute("PRAGMA ignore_check_constraints=ON")
        database.execute("UPDATE source SET sha256='bad'")
    directory = catalog.generations_path / "malformed"
    manifest = json.loads((directory / "manifest.json").read_bytes())
    payload = (directory / "evidence.sqlite3").read_bytes()
    manifest["artifacts"][0].update(size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
    from reliable_memory import canonical_json_bytes

    (directory / "manifest.json").write_bytes(canonical_json_bytes(manifest))

    with pytest.raises(ValueError, match="Evidence Graph|source"):
        catalog.register("malformed")
    assert catalog.recover_orphans() == []


def test_registration_rejects_wrong_index_definition_with_expected_name(tmp_path):
    from generation_catalog import GenerationCatalog
    from reliable_memory import canonical_json_bytes

    catalog = GenerationCatalog(tmp_path / "state")
    _publish(catalog, "wrong-index")
    directory = catalog.generations_path / "wrong-index"
    artifact = directory / "evidence.sqlite3"
    with sqlite3.connect(artifact) as database:
        database.execute("DROP INDEX assertion_reverse")
        database.execute("CREATE INDEX assertion_reverse ON assertion(source_node_id)")
    manifest = json.loads((directory / "manifest.json").read_bytes())
    payload = artifact.read_bytes()
    manifest["artifacts"][0].update(size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
    (directory / "manifest.json").write_bytes(canonical_json_bytes(manifest))

    with pytest.raises(ValueError, match="index|schema"):
        catalog.register("wrong-index")


def test_graph_extra_source_fields_remain_independently_validated(tmp_path):
    from generation_catalog import GenerationCatalog

    catalog = GenerationCatalog(tmp_path / "state")
    _publish(catalog, "bad-extra")
    with sqlite3.connect(catalog.generations_path / "bad-extra/evidence.sqlite3") as database:
        database.execute("UPDATE source SET media_type='' ")
    directory = catalog.generations_path / "bad-extra"
    manifest = json.loads((directory / "manifest.json").read_bytes())
    artifact = directory / "evidence.sqlite3"
    payload = artifact.read_bytes()
    graph_artifact = next(
        item for item in manifest["artifacts"] if item["path"] == "evidence.sqlite3"
    )
    graph_artifact.update(size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
    from reliable_memory import canonical_json_bytes

    (directory / "manifest.json").write_bytes(canonical_json_bytes(manifest))

    with pytest.raises(ValueError, match="source|controlled"):
        catalog.register("bad-extra")


def test_registration_accepts_canonical_multi_source_membership_and_rejects_mismatch(tmp_path):
    import corpus_snapshot
    from generation_catalog import GenerationCatalog
    from reliable_memory import canonical_json_bytes

    first = b"alpha"
    second = b"beta"
    sources = [
        {
            "source_id": "z-source",
            "relative_path": "z.py",
            "sha256": hashlib.sha256(first).hexdigest(),
            "size": len(first),
            "media_type": "text/x-python",
            "language": "python",
            "git_oid": None,
        },
        {
            "source_id": "a-source",
            "relative_path": "a.md",
            "sha256": hashlib.sha256(second).hexdigest(),
            "size": len(second),
            "media_type": "text/markdown",
            "language": "markdown",
            "git_oid": "abc123",
        },
    ]
    catalog = GenerationCatalog(tmp_path / "state")
    manifest = _publish(
        catalog,
        "multi",
        sources=list(reversed(sources)),
        source_bytes={"z-source": first, "a-source": second},
    )
    shared_sources = [
        {
            "logical_id": source["source_id"],
            "relative_path": source["relative_path"],
            "sha256": source["sha256"],
        }
        for source in sources
    ]
    shared_policy = {
        "daily_paths": [],
        "code_roots": [],
        "include_historical": False,
        "as_of": None,
    }
    assert manifest["source_manifest_sha256"] == corpus_snapshot.canonical_source_manifest_sha256(
        shared_sources, shared_policy
    )
    assert catalog.register("multi") == manifest

    mismatch = _publish(
        catalog,
        "mismatch",
        sources=sources,
        source_bytes={"z-source": first, "a-source": second},
    )
    mismatch["source_manifest_sha256"] = "0" * 64
    path = catalog.generations_path / "mismatch/manifest.json"
    path.write_bytes(canonical_json_bytes(mismatch))
    with pytest.raises(ValueError, match="source manifest|membership"):
        catalog.register("mismatch")


def test_source_membership_drift_falls_back_and_orphan_probe_skips_mismatch(tmp_path):
    import evidence_graph
    from generation_catalog import GenerationCatalog
    from reliable_memory import canonical_json_bytes

    catalog = GenerationCatalog(tmp_path / "state")
    _publish(catalog, "gen-1")
    _publish(catalog, "gen-2", parent="gen-1")
    for identifier in ("gen-1", "gen-2"):
        catalog.register(identifier)
    assert catalog.activate("gen-1", expected_active=None)
    assert catalog.activate("gen-2", expected_active="gen-1")
    with sqlite3.connect(catalog.generations_path / "gen-2/evidence.sqlite3") as database:
        database.execute("UPDATE source SET sha256=?", ("f" * 64,))
    _rebind_registered_manifest(catalog, "gen-2")

    graph = evidence_graph.EvidenceGraph.open_active(catalog)
    assert graph is not None
    assert graph.generation_id == "gen-1"
    graph.close()

    _publish(catalog, "activate-mismatch", parent="gen-1")
    catalog.register("activate-mismatch")
    with sqlite3.connect(
        catalog.generations_path / "activate-mismatch/evidence.sqlite3"
    ) as database:
        database.execute("UPDATE source SET sha256=?", ("d" * 64,))
    _rebind_registered_manifest(catalog, "activate-mismatch")
    with pytest.raises(ValueError, match="source manifest|membership"):
        catalog.activate("activate-mismatch", expected_active="gen-1")

    orphan = _publish(catalog, "orphan")
    orphan["source_manifest_sha256"] = "e" * 64
    orphan_path = catalog.generations_path / "orphan/manifest.json"
    orphan_path.write_bytes(canonical_json_bytes(orphan))
    assert catalog.recover_orphans() == []


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE source SET size=size+100",
        "UPDATE source SET content=X'00'",
        "UPDATE source SET relative_path='../app.py'",
        "UPDATE node SET kind='' WHERE node_id='caller'",
        "UPDATE node SET node_id='caller,alias' WHERE node_id='caller'",
        "UPDATE node SET metadata_json='{\"b\":1,\"a\":2}' WHERE node_id='caller'",
        "UPDATE occurrence SET role=''",
        "UPDATE occurrence SET byte_end=100",
        "UPDATE assertion SET extractor=''",
        "UPDATE assertion SET resolution='invented'",
        "UPDATE assertion SET target_node_id=NULL, "
        "literal_json='{\"b\":1,\"a\":2}', resolution='unresolved'",
        "UPDATE evidence SET byte_end=100, span_sha256='" + "0" * 64 + "'",
        "UPDATE evidence SET span_sha256='" + "0" * 64 + "'",
        "UPDATE observation SET extractor=''",
        "UPDATE observation SET reason='invented'",
        "UPDATE dependency SET kind=''",
        "DELETE FROM evidence",
        "DELETE FROM node WHERE node_id='callee'",
    ],
)
def test_hash_rebound_malformed_records_fall_back_to_prior_valid_generation(tmp_path, mutation):
    import evidence_graph
    from generation_catalog import GenerationCatalog

    catalog = GenerationCatalog(tmp_path / "state")
    _publish(catalog, "valid", graph_records=_rich_graph_records())
    _publish(catalog, "tampered", parent="valid", graph_records=_rich_graph_records())
    for generation_id in ("valid", "tampered"):
        catalog.register(generation_id)
    assert catalog.activate("valid", expected_active=None)
    assert catalog.activate("tampered", expected_active="valid")
    with sqlite3.connect(catalog.generations_path / "tampered/evidence.sqlite3") as database:
        database.execute("PRAGMA ignore_check_constraints=ON")
        database.execute(mutation)
    _rebind_registered_manifest(catalog, "tampered")

    graph = evidence_graph.EvidenceGraph.open_active(catalog)

    assert graph is not None
    assert graph.generation_id == "valid"
    assert catalog.get_active()["generation_id"] == "valid"
    graph.close()
