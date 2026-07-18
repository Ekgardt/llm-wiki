"""Read-only extraction of canonical project journals into graph records."""

from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Callable, Mapping, Sequence

from corpus_snapshot import CapturedSource
from knowledge_extractor import ExtractionResult, _evidence, _identifier, _node, _occurrence
from project_journal import parse_journal_events
from reliable_memory import canonical_json_bytes

EXTRACTOR_VERSION = "project-extractor/v1"
MAX_SOURCES = 10_000
MAX_RECORDS = 100_000


def _check_deadline(deadline: float | None, monotonic: Callable[[], float]) -> None:
    if deadline is not None and (
        isinstance(deadline, bool)
        or not isinstance(deadline, (int, float))
        or not math.isfinite(deadline)
    ):
        raise ValueError("deadline must be a finite monotonic timestamp")
    if deadline is not None and monotonic() >= deadline:
        raise TimeoutError("project extraction deadline reached")


def _check_stop(
    deadline: float | None,
    monotonic: Callable[[], float],
    cancelled: Callable[[], bool] | None,
) -> None:
    _check_deadline(deadline, monotonic)
    if cancelled is not None and cancelled():
        raise TimeoutError("project extraction cancelled")


def extract_projects(
    sources: Sequence[CapturedSource],
    *,
    max_sources: int = MAX_SOURCES,
    max_records: int = MAX_RECORDS,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    cancelled: Callable[[], bool] | None = None,
) -> ExtractionResult:
    """Project journal projection for graph construction; never writes the journal."""
    if isinstance(sources, (bytes, str)) or not isinstance(sources, Sequence):
        raise TypeError("sources must be a sequence of CapturedSource values")
    if not isinstance(max_sources, int) or isinstance(max_sources, bool) or max_sources < 1:
        raise ValueError("max_sources must be positive")
    if not isinstance(max_records, int) or isinstance(max_records, bool) or max_records < 1:
        raise ValueError("max_records must be positive")
    if cancelled is not None and not callable(cancelled):
        raise TypeError("cancelled must be callable")
    if len(sources) > min(max_sources, MAX_SOURCES):
        raise ValueError("project extraction source ceiling exceeded")
    _check_stop(deadline, monotonic, cancelled)
    paths = [item.record.relative_path for item in sources if isinstance(item, CapturedSource)]
    source_ids = [item.record.logical_id for item in sources if isinstance(item, CapturedSource)]
    if len(paths) != len(sources) or len(paths) != len(set(paths)) or len(source_ids) != len(set(source_ids)):
        raise ValueError("captured sources must have unique paths and logical IDs")
    for item in sources:
        if item.record.size != len(item.content) or item.record.sha256 != hashlib.sha256(item.content).hexdigest():
            raise ValueError("captured source bytes do not match immutable metadata")
    record_limit = min(max_records, MAX_RECORDS)

    nodes: dict[str, dict[str, object]] = {}
    occurrences: dict[str, dict[str, object]] = {}
    assertions: dict[str, dict[str, object]] = {}
    evidence: dict[str, dict[str, object]] = {}

    def check_work() -> None:
        _check_stop(deadline, monotonic, cancelled)
        if sum(map(len, (nodes, occurrences, assertions, evidence))) >= record_limit:
            raise ValueError("project extraction record ceiling exceeded")

    def relation(source: CapturedSource, source_node: str, edge: str, target: str, start: int, end: int) -> None:
        assertion_id = _identifier(
            "assertion",
            f"{source.record.logical_id}:{source_node}:{edge}:{target}:{start}:{end}",
        )
        assertions[assertion_id] = {
            "assertion_id": assertion_id,
            "source_node_id": source_node,
            "edge_type": edge,
            "target_node_id": target,
            "literal": None,
            "confidence": "high",
            "authority": "user",
            "resolution": "resolved",
            "extractor": EXTRACTOR_VERSION,
        }
        row = _evidence(source, assertion_id, start, end)
        evidence[str(row["evidence_id"])] = row

    for source in sorted(sources, key=lambda item: item.record.relative_path):
        if not isinstance(source, CapturedSource):
            raise TypeError("sources must contain CapturedSource values")
        check_work()
        path = source.record.relative_path
        if not path.endswith("/journal.md") or "/projects/" not in path:
            continue
        slug = path.rsplit("/", 2)[-2]
        project_id = _identifier("project", slug)
        nodes[project_id] = _node(project_id, "project", "project-slug/v1", slug)
        events = parse_journal_events(slug, source.content)
        search_start = 0
        for event in events:
            check_work()
            encoded = canonical_json_bytes(event)
            event_start = source.content.find(encoded, search_start)
            if event_start < 0:
                raise ValueError("canonical project event is not present in source bytes")
            event_end = event_start + len(encoded)
            search_start = event_end
            checkpoint_id = _identifier("checkpoint", f"{slug}:{event['sequence']}")
            session = str(event["provenance"]["session"])
            session_id = _identifier("session", session)
            nodes[checkpoint_id] = _node(
                checkpoint_id, "checkpoint", "project-sequence/v1",
                f"{slug}:{event['sequence']}", sequence=event["sequence"],
            )
            nodes[session_id] = _node(session_id, "session", "session-id/v1", session)
            occurrence = _occurrence(source, checkpoint_id, "event", event_start, event_end)
            occurrences[str(occurrence["occurrence_id"])] = occurrence
            relation(source, project_id, "PROJECT_HAS_CHECKPOINT", checkpoint_id, event_start, event_end)

            delta = event["delta"]
            assert isinstance(delta, Mapping)
            families = (
                ("changed_files", "file", "CHECKPOINT_CHANGED_FILE"),
                ("decisions", "decision", "CHECKPOINT_RECORDED_DECISION"),
                ("blockers", "blocker", "CHECKPOINT_HAS_BLOCKER"),
            )
            cursor = event_start
            for field, kind, edge in families:
                operations = delta[field]
                assert isinstance(operations, list)
                for operation in operations:
                    check_work()
                    if operation["action"] != "upsert":
                        continue
                    value = str(operation["value"])
                    token = canonical_json_bytes(value)
                    start = source.content.find(token, cursor, event_end)
                    if start < 0:
                        start = source.content.find(token, event_start, event_end)
                    if start < 0:
                        raise ValueError("project operation literal is absent from event bytes")
                    end = start + len(token)
                    cursor = end
                    target_id = _identifier(kind, f"{slug}:{operation['id']}")
                    nodes[target_id] = _node(target_id, kind, f"project-{kind}-id/v1", f"{slug}:{operation['id']}", value=value)
                    relation(source, checkpoint_id, edge, target_id, start, end)

            for event_id in event["evidence_event_ids"]:
                check_work()
                token = canonical_json_bytes(event_id)
                start = source.content.find(token, event_start, event_end)
                if start < 0:
                    raise ValueError("project evidence event ID is absent from event bytes")
                end = start + len(token)
                target_id = _identifier("event", str(event_id))
                nodes[target_id] = _node(target_id, "evidence", "event-id/v1", str(event_id))
                relation(source, checkpoint_id, "CHECKPOINT_EVIDENCED_BY_EVENT", target_id, start, end)

            if sum(map(len, (nodes, occurrences, assertions, evidence))) > record_limit:
                raise ValueError("project extraction record ceiling exceeded")

    def order(
        rows: Mapping[str, dict[str, object]], key: str
    ) -> tuple[dict[str, object], ...]:
        return tuple(sorted(rows.values(), key=lambda row: str(row[key])))

    return ExtractionResult(
        order(nodes, "node_id"),
        order(occurrences, "occurrence_id"),
        order(assertions, "assertion_id"),
        order(evidence, "evidence_id"),
        (),
    )
