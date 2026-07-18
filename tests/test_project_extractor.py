"""Read-only project journal extraction into Evidence Graph records."""

from __future__ import annotations

import hashlib
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _event() -> dict[str, object]:
    close = {"id": "none", "action": "close", "value": ""}
    return {
        "schema_version": "project-checkpoint/v1",
        "occurrence_id": "checkpoint-1",
        "idempotency_key": "task:one",
        "project": "demo",
        "sequence": 1,
        "provenance": {
            "agent": "agent-a",
            "session": "session-7",
            "worktree": "D:/work/demo",
            "branch": "feature/demo",
            "source_event": "event-9",
        },
        "trigger": "task_completed",
        "reason": "durable progress",
        "delta": {
            "goal": close,
            "phase": close,
            "current_task": close,
            "next_actions": [],
            "decisions": [{"id": "decision-1", "action": "upsert", "value": "Keep bytes immutable"}],
            "blockers": [{"id": "blocker-1", "action": "upsert", "value": "Waiting for Task19"}],
            "changed_files": [{"id": "file-1", "action": "upsert", "value": "scripts/project_journal.py"}],
            "commands": [],
            "verification": [],
        },
        "evidence_event_ids": ["event-8", "event-9"],
        "last_applied_sequence": 1,
    }


def _journal():
    from corpus_snapshot import CapturedSource, SourceMetadata, SourceRecord
    from project_journal import JOURNAL_HEADER
    from reliable_memory import canonical_json_bytes

    content = JOURNAL_HEADER.encode() + canonical_json_bytes(_event()) + b"\n"
    path = "knowledge/projects/demo/journal.md"
    source = CapturedSource(
        SourceRecord(
            f"source:{path}", path, hashlib.sha256(content).hexdigest(), len(content),
            "text/markdown", "markdown", None,
        ),
        SourceMetadata("project-journal", project="demo"),
        content,
    )
    return source


def test_projects_journal_edges_without_mutating_authoritative_bytes():
    from project_extractor import extract_projects

    source = _journal()
    before = source.content
    result = extract_projects((source,))

    assert source.content == before
    assert {node["kind"] for node in result.nodes} >= {
        "project",
        "checkpoint",
        "session",
        "file",
        "decision",
        "blocker",
        "evidence",
    }
    assert {row["edge_type"] for row in result.assertions} == {
        "PROJECT_HAS_CHECKPOINT",
        "CHECKPOINT_CHANGED_FILE",
        "CHECKPOINT_RECORDED_DECISION",
        "CHECKPOINT_HAS_BLOCKER",
        "CHECKPOINT_EVIDENCED_BY_EVENT",
    }
    for evidence in result.evidence:
        span = before[evidence["byte_start"] : evidence["byte_end"]]
        assert span
        assert hashlib.sha256(span).hexdigest() == evidence["span_sha256"]
    evidence_event_edges = {
        row["assertion_id"]
        for row in result.assertions
        if row["edge_type"] == "CHECKPOINT_EVIDENCED_BY_EVENT"
    }
    evidence_marker = before.index(b'"evidence_event_ids"')
    assert all(
        row["byte_start"] > evidence_marker
        for row in result.evidence
        if row["assertion_id"] in evidence_event_edges
    )


def test_public_journal_parser_is_read_only_validated_and_deterministic():
    from project_journal import parse_journal_events

    source = _journal()
    assert parse_journal_events("demo", source.content) == parse_journal_events(
        "demo", source.content
    )
    assert source.content == _journal().content
    with pytest.raises(ValueError, match="slug"):
        parse_journal_events("other", source.content)


def test_project_extraction_honors_source_and_deadline_bounds():
    from project_extractor import extract_projects

    source = _journal()
    with pytest.raises(ValueError, match="source ceiling"):
        extract_projects((source, source), max_sources=1)
    with pytest.raises(TimeoutError, match="deadline"):
        extract_projects((source,), deadline=time.monotonic() - 1)


def test_project_output_is_directly_accepted_by_evidence_graph_v1(tmp_path):
    import evidence_graph
    from project_extractor import extract_projects

    source = _journal()
    result = extract_projects((source,))
    record = source.record
    evidence_graph.create_generation_database(
        tmp_path / "evidence.sqlite3",
        sources=[
            {
                "source_id": record.logical_id,
                "relative_path": record.relative_path,
                "sha256": record.sha256,
                "size": record.size,
                "media_type": record.media_type,
                "language": record.language,
                "git_oid": record.git_oid,
            }
        ],
        source_bytes={record.logical_id: source.content},
        nodes=result.nodes,
        occurrences=result.occurrences,
        assertions=result.assertions,
        evidence=result.evidence,
        observations=result.observations,
        dependencies=result.dependencies,
    )
