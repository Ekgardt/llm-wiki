"""Helpers shared by test_whole_graph_aggregates.py and the test files importing them."""
from __future__ import annotations

import hashlib

import pytest

SOURCE = b"def hub(): pass\ndef warm(): hub()\ndef lonely(): pass\n"


_CALLS = (
    ("a1", "warm", "hub"),
    ("a2", "warm", "hub"),
    ("a3", "warm", "hub"),
    ("a4", "gate", "hub"),
    ("a5", "gate", "hub"),
    ("a6", "loop", "hub"),
    ("a7", "hub", "warm"),
    ("a8", "gate", "warm"),
    ("a9", "gate", "warm"),
    ("a10", "loop", "gate"),
    ("a11", "loop", "gate"),
    ("a12", "loop", "loop"),
)


_FUNCTIONS = (
    ("hub", "app.py"),
    ("warm", "app.py"),
    ("gate", "app.py"),
    ("loop", "app.py"),
    ("lonely", "app.py"),
    ("stale", "lib/util.py"),
    ("exposed", "app.py"),
    ("test_only", "app.py"),
    ("helper", "tests/test_app.py"),
)


def _node(name: str, path: str) -> dict:
    return {
        "node_id": name,
        "kind": "function",
        "identity_scheme": "python/v1",
        "identity_key": f"app:{name}",
        "metadata": {"name": name, "owner": "app", "path": path},
    }


def _assertion(identifier: str, source: str, target: str, edge_type: str) -> dict:
    return {
        "assertion_id": identifier,
        "source_node_id": source,
        "edge_type": edge_type,
        "target_node_id": target,
        "literal": None,
        "confidence": "high",
        "authority": "ai-derived",
        "resolution": "resolved",
        "extractor": "python/v1",
    }


def _graph_records() -> dict:
    assertions = [
        _assertion(identifier, source, target, "CALLS")
        for identifier, source, target in _CALLS
    ]
    assertions.append(_assertion("expose", "exposed", "hub", "EXPOSES"))
    return {
        "sources": [
            {
                "source_id": "source",
                "relative_path": "app.py",
                "sha256": hashlib.sha256(SOURCE).hexdigest(),
                "size": len(SOURCE),
                "media_type": "text/x-python",
                "language": "python",
                "git_oid": None,
            }
        ],
        "source_bytes": {"source": SOURCE},
        "nodes": [_node(name, path) for name, path in _FUNCTIONS],
        "occurrences": [
            {
                "occurrence_id": "occurrence",
                "node_id": "lonely",
                "source_id": "source",
                "role": "definition",
                "byte_start": 34,
                "byte_end": 52,
                "line_start": 3,
                "line_end": 3,
            }
        ],
        "assertions": assertions,
        "evidence": [
            {
                "evidence_id": f"evidence-{record['assertion_id']}",
                "assertion_id": record["assertion_id"],
                "observation_id": None,
                "source_id": "source",
                "byte_start": 16,
                "byte_end": 33,
                "span_sha256": hashlib.sha256(SOURCE[16:33]).hexdigest(),
            }
            for record in assertions
        ],
        "observations": [],
        "dependencies": [],
    }


@pytest.fixture()
def graph(tmp_path):
    from llm_wiki.generation_catalog_store import GenerationCatalog
    from repository_scope import resolve_repository_scope

    from tests.evidence_graph_recovery_helpers import _publish

    catalog = GenerationCatalog(tmp_path / "state")
    _publish(
        catalog,
        "active",
        graph_records=_graph_records(),
        repository_scope=resolve_repository_scope(tmp_path).as_dict(),
    )
    catalog.register("active")
    catalog.activate("active", expected_active=None)
    opened = _open(catalog, tmp_path)
    try:
        yield opened
    finally:
        opened.close()


def _open(catalog, directory):
    import evidence_graph
    from repository_scope import resolve_repository_scope

    return evidence_graph.EvidenceGraph.open_active_for_repository(
        catalog, resolve_repository_scope(directory)
    )
