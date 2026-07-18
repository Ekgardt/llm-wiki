"""Evidence Graph generation schema, validation, and query contracts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _records(source: bytes = b"def caller():\n    callee()\n# [[Decision]]\n"):
    decision_start = source.index(b"[[Decision]]")
    return {
        "sources": [
            {
                "source_id": "src-code",
                "relative_path": "src/app.py",
                "sha256": _sha(source),
                "size": len(source),
                "media_type": "text/x-python",
                "language": "python",
                "git_oid": None,
            }
        ],
        "source_bytes": {"src-code": source},
        "nodes": [
            {
                "node_id": "caller",
                "kind": "function",
                "identity_scheme": "python-qualified-name/v1",
                "identity_key": "src.app:caller",
                "metadata": {"name": "caller"},
            },
            {
                "node_id": "callee",
                "kind": "function",
                "identity_scheme": "python-qualified-name/v1",
                "identity_key": "src.app:callee",
                "metadata": {"name": "callee"},
            },
            {
                "node_id": "decision",
                "kind": "decision",
                "identity_scheme": "wiki-slug/v1",
                "identity_key": "decision",
                "metadata": {},
            },
        ],
        "occurrences": [
            {
                "occurrence_id": "occ-caller",
                "node_id": "caller",
                "source_id": "src-code",
                "role": "definition",
                "byte_start": 0,
                "byte_end": 13,
                "line_start": 1,
                "line_end": 1,
            }
        ],
        "assertions": [
            {
                "assertion_id": "call",
                "source_node_id": "caller",
                "edge_type": "CALLS",
                "target_node_id": "callee",
                "literal": None,
                "confidence": "high",
                "authority": "ai-derived",
                "resolution": "resolved",
                "extractor": "python/v1",
            },
            {
                "assertion_id": "documents",
                "source_node_id": "decision",
                "edge_type": "DOCUMENTS",
                "target_node_id": "caller",
                "literal": None,
                "confidence": "high",
                "authority": "user",
                "resolution": "resolved",
                "extractor": "wiki/v1",
            },
        ],
        "observations": [
            {
                "observation_id": "dynamic-call",
                "source_node_id": "caller",
                "edge_type": "CALLS",
                "target_text": "runtime_target",
                "reason": "dynamic_dispatch",
                "extractor": "python/v1",
            }
        ],
        "evidence": [
            {
                "evidence_id": "ev-call",
                "assertion_id": "call",
                "observation_id": None,
                "source_id": "src-code",
                "byte_start": 18,
                "byte_end": 26,
                "span_sha256": _sha(source[18:26]),
            },
            {
                "evidence_id": "ev-documents",
                "assertion_id": "documents",
                "observation_id": None,
                "source_id": "src-code",
                "byte_start": decision_start,
                "byte_end": decision_start + len(b"[[Decision]]"),
                "span_sha256": _sha(b"[[Decision]]"),
            }
        ],
        "dependencies": [
            {
                "dependency_id": "dep-code",
                "dependent_node_id": "decision",
                "dependency_node_id": "caller",
                "kind": "documents",
                "source_id": "src-code",
            }
        ],
    }


def _create(tmp_path: Path, **overrides):
    import evidence_graph

    records = _records()
    records.update(overrides)
    path = tmp_path / "evidence.sqlite3"
    evidence_graph.create_generation_database(path, **records)
    return evidence_graph.EvidenceGraph(path, state_root=tmp_path)


def test_manifest_schema_is_closed_and_bounded():
    from reliable_memory import validate_schema

    schema = json.loads((SCRIPTS / "schemas/evidence-graph-manifest-v1.json").read_text())
    valid = {
        "generation_id": "gen-1",
        "schema_version": "corpus-generation/v1",
        "collector_version": "collector/v1",
        "extractor_version": "extractor/v1",
        "tokenizer_version": "tokenizer/v1",
        "tokenizer_config_sha256": "0" * 64,
        "embedding_model_id": None,
        "embedding_model_revision": None,
        "vector_dimensions": None,
        "graph_schema_version": "evidence-graph/v1",
        "graph_extractor_version": "graph-extractor/v1",
        "source_manifest_sha256": "1" * 64,
        "artifacts": [
            {"path": "evidence.sqlite3", "size": 4096, "sha256": "2" * 64},
            {"path": "source-manifest.json", "size": 1024, "sha256": "1" * 64},
        ],
        "vector_state": "absent",
    }

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False
    validate_schema(valid, SCRIPTS / "schemas/evidence-graph-manifest-v1.json")
    with pytest.raises(ValueError, match="const|graph_schema_version"):
        validate_schema(
            {**valid, "graph_schema_version": "other/v1"},
            SCRIPTS / "schemas/evidence-graph-manifest-v1.json",
        )
    with pytest.raises(ValueError, match="unknown|additional"):
        validate_schema(
            {**valid, "sources": []}, SCRIPTS / "schemas/evidence-graph-manifest-v1.json"
        )


def test_graph_source_rows_use_shared_corpus_manifest_contract():
    import corpus_snapshot

    sources = _records()["sources"]
    second = {
        "source_id": "src-doc",
        "relative_path": "knowledge/notes/decision.md",
        "sha256": _sha(b"decision"),
        "size": len(b"decision"),
        "media_type": "text/markdown",
        "language": "markdown",
        "git_oid": "abc123",
    }
    shared = [
        {
            "logical_id": source["source_id"],
            "relative_path": source["relative_path"],
            "sha256": source["sha256"],
        }
        for source in [second, *sources]
    ]
    policy = {"daily_paths": [], "code_roots": [], "include_historical": False, "as_of": None}

    forward = corpus_snapshot.canonical_source_manifest_sha256(shared, policy)
    reverse = corpus_snapshot.canonical_source_manifest_sha256(reversed(shared), policy)

    assert forward == reverse


def test_generation_database_has_canonical_tables_indexes_and_pragmas(tmp_path):
    graph = _create(tmp_path)
    graph.close()

    with closing(sqlite3.connect(tmp_path / "evidence.sqlite3")) as database:
        tables = {
            row[0]
            for row in database.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        }
        indexes = {
            row[0]
            for row in database.execute(
                "SELECT name FROM sqlite_master WHERE type='index' ORDER BY name"
            )
        }
        assert database.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
        assert database.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert database.execute("PRAGMA user_version").fetchone()[0] == 1
        assert database.execute("PRAGMA foreign_key_check").fetchall() == []

    assert tables == {
        "assertion",
        "dependency",
        "evidence",
        "node",
        "observation",
        "occurrence",
        "source",
    }
    assert {
        "assertion_reverse",
        "assertion_resolution",
        "assertion_traversal",
        "dependency_invalidation",
        "dependency_reverse",
        "evidence_source_span",
        "node_kind",
        "observation_resolution",
        "occurrence_source_span",
    } <= indexes
    assert not (tmp_path / "evidence.sqlite3-wal").exists()


def test_logical_nodes_are_separate_from_occurrences_and_metadata_is_canonical(tmp_path):
    graph = _create(tmp_path)

    node = graph.node("caller")
    occurrences = graph.occurrences("caller")

    assert node["identity_key"] == "src.app:caller"
    assert node["metadata"] == {"name": "caller"}
    assert occurrences == [
        {
            "occurrence_id": "occ-caller",
            "node_id": "caller",
            "source_id": "src-code",
            "relative_path": "src/app.py",
            "role": "definition",
            "byte_start": 0,
            "byte_end": 13,
            "line_start": 1,
            "line_end": 1,
        }
    ]
    graph.close()


@pytest.mark.parametrize("damage", ["source_hash", "range", "span_hash", "unknown_field"])
def test_create_fails_closed_on_invalid_sources_evidence_and_records(tmp_path, damage):
    import evidence_graph

    records = _records()
    if damage == "source_hash":
        records["sources"][0]["sha256"] = "0" * 64
    elif damage == "range":
        records["evidence"][0]["byte_end"] = len(records["source_bytes"]["src-code"]) + 1
    elif damage == "span_hash":
        records["evidence"][0]["span_sha256"] = "0" * 64
    else:
        records["nodes"][0]["unknown"] = True

    with pytest.raises((TypeError, ValueError), match="hash|range|unknown|closed"):
        evidence_graph.create_generation_database(tmp_path / "evidence.sqlite3", **records)
    assert not (tmp_path / "evidence.sqlite3").exists()


def test_every_resolved_assertion_requires_nonempty_half_open_evidence(tmp_path):
    import evidence_graph

    records = _records()
    records["evidence"] = records["evidence"][:1]
    with pytest.raises(ValueError, match="resolved assertion.*evidence"):
        evidence_graph.create_generation_database(tmp_path / "missing.sqlite3", **records)

    records = _records()
    records["evidence"][0].update(
        byte_end=records["evidence"][0]["byte_start"], span_sha256=_sha(b"")
    )
    with pytest.raises(ValueError, match="non-empty|range"):
        evidence_graph.create_generation_database(tmp_path / "empty.sqlite3", **records)

    records = _records()
    records["evidence"][0]["byte_end"] += 1
    records["evidence"][0]["span_sha256"] = _sha(b"callee()\n")
    evidence_graph.create_generation_database(tmp_path / "half-open.sqlite3", **records)


def test_evidence_source_binding_is_verified(tmp_path):
    import evidence_graph

    records = _records()
    other = b"XXXXXXXX"
    records["sources"].append(
        {
            "source_id": "other",
            "relative_path": "other.py",
            "sha256": _sha(other),
            "size": len(other),
            "media_type": "text/x-python",
            "language": "python",
            "git_oid": None,
        }
    )
    records["source_bytes"]["other"] = other
    records["evidence"][0].update(source_id="other", byte_start=0, byte_end=len(other))
    with pytest.raises(ValueError, match="source|hash"):
        evidence_graph.create_generation_database(tmp_path / "wrong-source.sqlite3", **records)


def test_generation_publication_is_exclusive_under_concurrent_destination_race(tmp_path):
    import evidence_graph

    destination = tmp_path / "evidence.sqlite3"

    def publish():
        try:
            evidence_graph.create_generation_database(destination, **_records())
        except FileExistsError:
            return "lost"
        return "won"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(lambda _value: publish(), range(2)))

    assert results == ["lost", "won"]
    graph = evidence_graph.EvidenceGraph(destination, state_root=tmp_path)
    assert graph.node("caller")["identity_key"] == "src.app:caller"
    graph.close()


def test_unresolved_observations_use_controlled_reasons_without_fake_nodes(tmp_path):
    graph = _create(tmp_path)

    assert graph.unresolved() == [
        {
            "observation_id": "dynamic-call",
            "source_node_id": "caller",
            "edge_type": "CALLS",
            "target_text": "runtime_target",
            "reason": "dynamic_dispatch",
            "extractor": "python/v1",
        }
    ]
    graph.close()

    records = _records()
    records["observations"][0]["reason"] = "made_up"
    with pytest.raises(ValueError, match="reason"):
        import evidence_graph

        evidence_graph.create_generation_database(tmp_path / "bad.sqlite3", **records)


def test_bounded_queries_cover_both_directions_paths_dependencies_and_evidence(tmp_path):
    graph = _create(tmp_path)

    assert [row["node_id"] for row in graph.neighbors("caller", direction="out")] == [
        "callee"
    ]
    assert [row["node_id"] for row in graph.neighbors("callee", direction="in")] == [
        "caller"
    ]
    assert graph.path("caller", "callee")[0]["assertion_ids"] == ["call"]
    assert [row["node_id"] for row in graph.callers("callee")] == ["caller"]
    assert [row["node_id"] for row in graph.callees("caller")] == ["callee"]
    assert [row["node_id"] for row in graph.dependencies("decision")] == ["caller"]
    assert [row["node_id"] for row in graph.code_to_doc("caller")] == ["decision"]
    assert [row["node_id"] for row in graph.doc_to_code("decision")] == ["caller"]
    assert graph.evidence(assertion_id="call")[0]["span_sha256"] == _sha(b"callee()")
    graph.close()


def test_dependencies_walk_transitively_with_a_depth_bound(tmp_path):
    records = _records()
    records["nodes"].append(
        {
            "node_id": "package",
            "kind": "package",
            "identity_scheme": "package/v1",
            "identity_key": "runtime",
            "metadata": {},
        }
    )
    records["dependencies"].append(
        {
            "dependency_id": "dep-runtime",
            "dependent_node_id": "caller",
            "dependency_node_id": "package",
            "kind": "imports",
            "source_id": "src-code",
        }
    )
    graph = _create(tmp_path, **records)

    assert [row["node_id"] for row in graph.dependencies("decision", max_depth=1)] == [
        "caller"
    ]
    assert [row["node_id"] for row in graph.dependencies("decision", max_depth=2)] == [
        "caller",
        "package",
    ]
    graph.close()


def test_query_limits_depth_rows_deadlines_and_closed_enums(tmp_path):
    graph = _create(tmp_path)

    with pytest.raises(ValueError, match="direction"):
        graph.neighbors("caller", direction="sideways")
    with pytest.raises(ValueError, match="max_depth"):
        graph.path("caller", "callee", max_depth=0)
    with pytest.raises(ValueError, match="max_rows"):
        graph.unresolved(max_rows=0)
    with pytest.raises(TimeoutError, match="deadline"):
        graph.neighbors("caller", deadline=time.monotonic() - 1)
    with pytest.raises(ValueError, match="edge_types"):
        graph.neighbors("caller", edge_types=[f"EDGE_{number}" for number in range(65)])
    graph.close()


def test_recursive_traversal_is_deterministic_delimiter_safe_and_work_bounded(tmp_path):
    import evidence_graph

    records = _records()
    records["nodes"].extend(
        {
            "node_id": identifier,
            "kind": "function",
            "identity_scheme": "test/v1",
            "identity_key": identifier,
            "metadata": {},
        }
        for identifier in ("branch-a", "branch-b", "branch-c")
    )
    for number, (source, target) in enumerate(
        (
            ("callee", "branch-c"),
            ("caller", "branch-b"),
            ("caller", "branch-a"),
            ("branch-a", "caller"),
        )
    ):
        assertion_id = f"edge-{number}"
        records["assertions"].append(
            {
                "assertion_id": assertion_id,
                "source_node_id": source,
                "edge_type": "CALLS",
                "target_node_id": target,
                "literal": None,
                "confidence": "high",
                "authority": "ai-derived",
                "resolution": "resolved",
                "extractor": "test/v1",
            }
        )
        records["evidence"].append(
            {
                "evidence_id": f"evidence-{number}",
                "assertion_id": assertion_id,
                "observation_id": None,
                "source_id": "src-code",
                "byte_start": 18,
                "byte_end": 26,
                "span_sha256": _sha(b"callee()"),
            }
        )
    graph = _create(tmp_path, **records)

    first = graph.neighbors("caller", max_depth=3, max_rows=10, max_work=20)
    second = graph.neighbors("caller", max_depth=3, max_rows=10, max_work=20)
    assert first == second
    assert [row["node_id"] for row in first] == ["branch-a", "branch-b", "callee", "branch-c"]
    with pytest.raises(ValueError, match="work"):
        graph.neighbors("caller", max_depth=3, max_rows=10, max_work=2)
    graph.close()

    bad = _records()
    bad["nodes"][0]["node_id"] = "caller,alias"
    with pytest.raises(ValueError, match="node_id"):
        evidence_graph.create_generation_database(tmp_path / "unsafe-id.sqlite3", **bad)


def test_database_is_opened_query_only_and_path_must_remain_in_state_root(tmp_path):
    graph = _create(tmp_path)
    assert graph._database.execute("PRAGMA query_only").fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError):
        graph._database.execute("DELETE FROM node")
    graph.close()

    import evidence_graph

    with pytest.raises((PermissionError, ValueError)):
        evidence_graph.EvidenceGraph(tmp_path / "evidence.sqlite3", state_root=tmp_path / "other")
