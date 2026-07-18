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


def _publish(catalog, generation_id: str, *, parent: str | None = None):
    import evidence_graph
    from reliable_memory import canonical_json_bytes

    directory = catalog.generations_path / generation_id
    directory.mkdir(parents=True)
    content = b"def f(): pass\n"
    database_path = directory / "evidence.sqlite3"
    evidence_graph.create_generation_database(
        database_path,
        sources=[
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
        "collector_version": "collector/v1",
        "extractor_version": "extractor/v1",
        "tokenizer_version": "tokenizer/v1",
        "tokenizer_config_sha256": hashlib.sha256(b"config").hexdigest(),
        "embedding_model_id": None,
        "embedding_model_revision": None,
        "vector_dimensions": None,
        "graph_schema_version": "evidence-graph/v1",
        "graph_extractor_version": "graph-extractor/v1",
        "source_manifest_sha256": hashlib.sha256(b"sources").hexdigest(),
        "artifacts": [
            {
                "path": "evidence.sqlite3",
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
        "vector_state": "absent",
    }
    if parent is not None:
        manifest["parent_generation_id"] = parent
    (directory / "manifest.json").write_bytes(canonical_json_bytes(manifest))
    return manifest


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
    manifest["artifacts"][0].update(size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
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
