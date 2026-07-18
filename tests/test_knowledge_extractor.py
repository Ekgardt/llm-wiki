"""Canonical knowledge-source extraction into Evidence Graph records."""

from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _source(path: str, content: bytes, *, kind: str = "concept"):
    from corpus_snapshot import CapturedSource, SourceMetadata, SourceRecord

    return CapturedSource(
        SourceRecord(
            f"source:{path}",
            path,
            hashlib.sha256(content).hexdigest(),
            len(content),
            "text/markdown",
            "markdown",
            None,
        ),
        SourceMetadata(kind),
        content,
    )


def test_extracts_typed_pages_projects_links_supersession_and_exact_spans():
    from knowledge_extractor import extract_knowledge

    old = (
        b"---\ntype: decision\nproject: atlas\nstatus: superseded\n"
        b"superseded_by: '[[new-choice]]'\n---\n# Old choice\n"
        b"See [[debug-note]] and [[new-choice]].\n"
    )
    new = b"---\ntype: decision\n---\n# New choice\n"
    debugging = b"---\ntype: debugging\n---\n# Debug note\n"
    before = (old, new, debugging)

    result = extract_knowledge(
        (
            _source("knowledge/notes/old-choice.md", old, kind="decision"),
            _source("knowledge/notes/new-choice.md", new, kind="decision"),
            _source("knowledge/notes/debug-note.md", debugging, kind="debugging"),
        )
    )

    assert before == (old, new, debugging)
    assert {node["kind"] for node in result.nodes} >= {
        "decision",
        "debugging-note",
        "project",
    }
    edges = {(row["edge_type"], row["source_node_id"], row["target_node_id"]) for row in result.assertions}
    assert any(edge[0] == "LINKS_TO" and edge[2].endswith("debug-note.md") for edge in edges)
    assert any(edge[0] == "BELONGS_TO_PROJECT" and edge[2] == "project:atlas" for edge in edges)
    assert any(
        edge[0] == "SUPERSEDES"
        and edge[1].endswith("new-choice.md")
        and edge[2].endswith("old-choice.md")
        for edge in edges
    )
    sources = {source.record.logical_id: source.content for source in (
        _source("knowledge/notes/old-choice.md", old, kind="decision"),
        _source("knowledge/notes/new-choice.md", new, kind="decision"),
        _source("knowledge/notes/debug-note.md", debugging, kind="debugging"),
    )}
    for evidence in result.evidence:
        span = sources[evidence["source_id"]][evidence["byte_start"] : evidence["byte_end"]]
        assert hashlib.sha256(span).hexdigest() == evidence["span_sha256"]


def test_symbol_references_require_explicit_identity_and_observe_bare_ambiguity():
    from knowledge_extractor import extract_knowledge

    content = (
        b"---\ntype: concept\n---\n# Calls\n"
        b"Use `scripts/api.py::serve`, `scip-python pkg api/serve().`, and `serve`.\n"
    )
    result = extract_knowledge(
        (_source("knowledge/notes/calls.md", content),),
        symbol_index={
            "scripts/api.py::serve": "symbol:path-serve",
            "scip-python pkg api/serve().": "symbol:scip-serve",
            "serve": "symbol:bare-serve",
        },
    )

    references = [row for row in result.assertions if row["edge_type"] == "REFERENCES_SYMBOL"]
    assert [row["target_node_id"] for row in references] == [
        "symbol:path-serve",
        "symbol:scip-serve",
    ]
    assert [(row["target_text"], row["reason"]) for row in result.observations] == [
        ("serve", "ambiguous_target")
    ]


def test_ambiguous_wikilinks_are_observations_and_work_is_bounded():
    from knowledge_extractor import extract_knowledge

    source = _source("knowledge/notes/source.md", b"---\ntype: concept\n---\n[[same]]\n")
    same_a = _source("knowledge/notes/a/same.md", b"---\ntype: concept\n---\n# A\n")
    same_b = _source("knowledge/notes/b/same.md", b"---\ntype: concept\n---\n# B\n")
    result = extract_knowledge((source, same_b, same_a))
    assert not [row for row in result.assertions if row["edge_type"] == "LINKS_TO"]
    assert result.observations[0]["reason"] == "ambiguous_target"

    with pytest.raises(ValueError, match="source ceiling"):
        extract_knowledge((source, same_a), max_sources=1)
    with pytest.raises(TimeoutError, match="deadline"):
        extract_knowledge((source,), deadline=time.monotonic() - 1)


def test_results_are_deterministic_independent_of_input_order():
    from knowledge_extractor import extract_knowledge

    a = _source("knowledge/notes/a.md", b"---\ntype: concept\n---\n[[b]] [[b]]\n")
    b = _source("knowledge/notes/b.md", b"---\ntype: concept\n---\n# B\n")
    assert extract_knowledge((a, b)) == extract_knowledge((b, a))


def test_claim_ledger_emits_claim_evidence_nodes_and_evidenced_by_edge():
    from claims import _semantic_payload
    from knowledge_extractor import extract_knowledge
    from reliable_memory import canonical_json_bytes

    literal = "Service uses SQLite"
    semantic = {
        "subject": "service",
        "relation": "uses",
        "value": {"type": "entity", "value": "sqlite"},
        "qualifiers": [],
        "validity": {"from": None, "to": None},
    }
    fingerprint = hashlib.sha256(canonical_json_bytes(_semantic_payload(semantic))).hexdigest()
    claim = {
        "schema_version": "claim/v1",
        "id": "claim-1",
        "fingerprint": fingerprint,
        "text": literal,
        **semantic,
        "observed_at": "2026-01-02T03:04:05Z",
        "lifecycle": "active",
        "confidence": "high",
        "authority": "user",
        "evidence": {
            "reference": f"daily:2026-01-02 sha256:{'0' * 64} block:03:04:05 bytes:0-{len(literal)}",
            "sha256": hashlib.sha256(literal.encode()).hexdigest(),
            "text": literal,
        },
        "links": [],
        "extractor_version": "extractor/v1",
    }
    ledger = {"claims": [claim], "schema_version": "claim-ledger/v1"}
    content = (
        b"---\ntype: concept\n---\n# Storage\n\n## Claims\n```json\n"
        + json.dumps(ledger, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        + b"\n```\n"
    )

    result = extract_knowledge((_source("knowledge/notes/storage.md", content),))

    assert {node["kind"] for node in result.nodes} >= {"claim", "evidence"}
    assert [row["edge_type"] for row in result.assertions] == ["EVIDENCED_BY"]


def test_rejects_tampered_captured_sources_and_record_overflow():
    from corpus_snapshot import CapturedSource
    from knowledge_extractor import extract_knowledge

    source = _source("knowledge/notes/a.md", b"---\ntype: concept\n---\n# A\n")
    tampered = CapturedSource(source.record, source.metadata, source.content + b"changed")
    with pytest.raises(ValueError, match="captured source"):
        extract_knowledge((tampered,))
    with pytest.raises(ValueError, match="record ceiling"):
        extract_knowledge((source,), max_records=1)


def test_output_is_directly_accepted_by_evidence_graph_v1(tmp_path):
    import evidence_graph
    from knowledge_extractor import extract_knowledge

    first = _source("knowledge/notes/a.md", b"---\ntype: concept\n---\n[[b]]\n")
    second = _source("knowledge/notes/b.md", b"---\ntype: debugging\n---\n# B\n")
    sources = (first, second)
    result = extract_knowledge(sources)

    evidence_graph.create_generation_database(
        tmp_path / "evidence.sqlite3",
        sources=[
            {
                "source_id": item.record.logical_id,
                "relative_path": item.record.relative_path,
                "sha256": item.record.sha256,
                "size": item.record.size,
                "media_type": item.record.media_type,
                "language": item.record.language,
                "git_oid": item.record.git_oid,
            }
            for item in sources
        ],
        source_bytes={item.record.logical_id: item.content for item in sources},
        nodes=result.nodes,
        occurrences=result.occurrences,
        assertions=result.assertions,
        evidence=result.evidence,
        observations=result.observations,
        dependencies=result.dependencies,
    )
