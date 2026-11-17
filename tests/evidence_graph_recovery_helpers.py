"""Helpers shared by test_evidence_graph_recovery.py and the test files importing them."""
from __future__ import annotations

import hashlib

_DEFAULT_CONTENT = b"def f(): pass\n"


_EMPTY_GRAPH_RECORDS = {
    "nodes": [],
    "occurrences": [],
    "assertions": [],
    "evidence": [],
    "observations": [],
    "dependencies": [],
}


def _default_sources():
    return [
        {
            "source_id": "source",
            "relative_path": "app.py",
            "sha256": hashlib.sha256(_DEFAULT_CONTENT).hexdigest(),
            "size": len(_DEFAULT_CONTENT),
            "media_type": "text/x-python",
            "language": "python",
            "git_oid": None,
        }
    ]


def _publish_inputs(sources, source_bytes, graph_records):
    """The three ways a caller can describe a generation, resolved to one shape."""
    records = _EMPTY_GRAPH_RECORDS if graph_records is None else graph_records
    if graph_records is not None:
        return graph_records["sources"], graph_records["source_bytes"], records
    resolved_sources = _default_sources() if sources is None else sources
    resolved_bytes = (
        {"source": _DEFAULT_CONTENT} if source_bytes is None else source_bytes
    )
    return resolved_sources, resolved_bytes, records


def _publish(
    catalog,
    generation_id: str,
    *,
    parent: str | None = None,
    sources=None,
    source_bytes=None,
    graph_records=None,
    repository_scope=None,
):
    import corpus_snapshot
    import evidence_graph
    from reliable_memory import canonical_json_bytes

    directory = catalog.generations_path / generation_id
    directory.mkdir(parents=True)
    sources, source_bytes, records = _publish_inputs(
        sources, source_bytes, graph_records
    )
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
        nodes=records["nodes"],
        occurrences=records["occurrences"],
        assertions=records["assertions"],
        evidence=records["evidence"],
        observations=records["observations"],
        dependencies=records["dependencies"],
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
    if repository_scope is not None:
        manifest["repository_scope"] = repository_scope
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
