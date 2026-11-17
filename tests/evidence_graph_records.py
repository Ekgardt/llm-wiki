"""A small generation every Evidence Graph test can build: one caller, one callee, one decision."""

from __future__ import annotations

import hashlib
from pathlib import Path

import evidence_graph


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def graph_records(source: bytes = b"def caller():\n    callee()\n# [[Decision]]\n"):
    decision_start = source.index(b"[[Decision]]")
    return {
        "sources": [
            {
                "source_id": "src-code",
                "relative_path": "src/app.py",
                "sha256": sha(source),
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
                "span_sha256": sha(source[18:26]),
            },
            {
                "evidence_id": "ev-documents",
                "assertion_id": "documents",
                "observation_id": None,
                "source_id": "src-code",
                "byte_start": decision_start,
                "byte_end": decision_start + len(b"[[Decision]]"),
                "span_sha256": sha(b"[[Decision]]"),
            },
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


def create_graph(tmp_path: Path, **overrides):
    records = graph_records()
    records.update(overrides)
    path = tmp_path / "evidence.sqlite3"
    evidence_graph.create_generation_database(path, **records)
    return evidence_graph.EvidenceGraph(path, state_root=tmp_path)


def basic_graph_records() -> dict[str, object]:
    content = b"def caller():\n    callee()\n"
    return {
        "sources": [
            {
                "source_id": "source:app.py",
                "relative_path": "app.py",
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
                "media_type": "text/x-python",
                "language": "python",
                "git_oid": None,
            }
        ],
        "source_bytes": {"source:app.py": content},
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
                "source_id": "source:app.py",
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
                "source_id": "source:app.py",
                "byte_start": 18,
                "byte_end": 26,
                "span_sha256": hashlib.sha256(content[18:26]).hexdigest(),
            }
        ],
        "observations": [],
        "dependencies": [],
    }


def build_fixture_generation(tmp_path: Path, *, generation_id: str, graph_schema=None):
    """One fixture generation of the published schema, built through the builder."""
    from evidence_graph_builder import build_full_generation
    from llm_wiki.generation_catalog_store import GenerationCatalog

    records = basic_graph_records()
    options = {} if graph_schema is None else {"graph_schema": graph_schema}
    return build_full_generation(
        GenerationCatalog(tmp_path / "state"),
        generation_id=generation_id,
        activate=False,
        **options,
        **records,
    )
