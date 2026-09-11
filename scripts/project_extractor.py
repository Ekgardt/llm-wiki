"""Read-only extraction of canonical project journals into graph records."""

from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from corpus_snapshot import CapturedSource
from knowledge_extractor import ExtractionResult, _evidence, _identifier, _node, _occurrence
from project_journal import parse_journal_events
from reliable_memory import canonical_json_bytes

EXTRACTOR_VERSION = "project-extractor/v1"
MAX_SOURCES = 10_000
MAX_RECORDS = 100_000


def _check_deadline(deadline: float | None, monotonic: Callable[[], float]) -> None:
    if deadline is None:
        return
    _require_finite_deadline(deadline)
    if monotonic() >= deadline:
        raise TimeoutError("project extraction deadline reached")


def _require_finite_deadline(deadline: object) -> None:
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
        raise ValueError("deadline must be a finite monotonic timestamp")


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
    _require_extraction_options(sources, max_sources, max_records, cancelled)
    _check_stop(deadline, monotonic, cancelled)
    _require_intact_sources(sources)
    extraction = _ProjectExtraction(
        min(max_records, MAX_RECORDS), lambda: _check_stop(deadline, monotonic, cancelled)
    )
    for source in sorted(sources, key=lambda item: item.record.relative_path):
        extraction.add_source(source)
    return extraction.result()


def _require_extraction_options(
    sources: object, max_sources: int, max_records: int, cancelled: Callable[[], bool] | None
) -> None:
    _require_source_sequence(sources)
    _require_positive("max_sources", max_sources)
    _require_positive("max_records", max_records)
    if cancelled is not None and not callable(cancelled):
        raise TypeError("cancelled must be callable")
    if len(sources) > min(max_sources, MAX_SOURCES):
        raise ValueError("project extraction source ceiling exceeded")


def _require_source_sequence(sources: object) -> None:
    if isinstance(sources, (bytes, str)) or not isinstance(sources, Sequence):
        raise TypeError("sources must be a sequence of CapturedSource values")


def _require_positive(name: str, value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be positive")


def _identities(sources: Sequence[CapturedSource]) -> tuple[list[str], list[str]]:
    captured = [item for item in sources if isinstance(item, CapturedSource)]
    return [item.record.relative_path for item in captured], [item.record.logical_id for item in captured]


def _duplicated(values: list[str]) -> bool:
    return len(values) != len(set(values))


def _require_intact_sources(sources: Sequence[CapturedSource]) -> None:
    """Every source is captured, uniquely named, and its bytes match its record."""
    paths, source_ids = _identities(sources)
    if len(paths) != len(sources) or _duplicated(paths) or _duplicated(source_ids):
        raise ValueError("captured sources must have unique paths and logical IDs")
    for item in sources:
        _require_recorded_bytes(item)


def _require_recorded_bytes(item: CapturedSource) -> None:
    if item.record.size != len(item.content) or item.record.sha256 != hashlib.sha256(item.content).hexdigest():
        raise ValueError("captured source bytes do not match immutable metadata")


_DELTA_FAMILIES = (
    ("changed_files", "file", "CHECKPOINT_CHANGED_FILE"),
    ("decisions", "decision", "CHECKPOINT_RECORDED_DECISION"),
    ("blockers", "blocker", "CHECKPOINT_HAS_BLOCKER"),
)


@dataclass(frozen=True)
class _EventSpan:
    """One journal event: its source, project, checkpoint node and byte span."""

    source: CapturedSource
    slug: str
    checkpoint_id: str
    start: int
    end: int


def _event_span(content: bytes, event: Mapping[str, object], search_start: int) -> tuple[int, int]:
    encoded = canonical_json_bytes(event)
    event_start = content.find(encoded, search_start)
    if event_start < 0:
        raise ValueError("canonical project event is not present in source bytes")
    return event_start, event_start + len(encoded)


def _literal_span(span: _EventSpan, token: bytes, cursor: int) -> tuple[int, int]:
    """The literal's bytes after the cursor, else anywhere in the event."""
    content = span.source.content
    start = content.find(token, cursor, span.end)
    if start < 0:
        start = content.find(token, span.start, span.end)
    if start < 0:
        raise ValueError("project operation literal is absent from event bytes")
    return start, start + len(token)


def _ordered(rows: Mapping[str, dict[str, object]], key: str) -> tuple[dict[str, object], ...]:
    return tuple(sorted(rows.values(), key=lambda row: str(row[key])))


class _ProjectExtraction:
    """Nodes, occurrences, assertions and evidence of every project journal, under one ceiling."""

    def __init__(self, record_limit: int, check_stop: Callable[[], None]) -> None:
        self.record_limit = record_limit
        self.check_stop = check_stop
        self.nodes: dict[str, dict[str, object]] = {}
        self.occurrences: dict[str, dict[str, object]] = {}
        self.assertions: dict[str, dict[str, object]] = {}
        self.evidence: dict[str, dict[str, object]] = {}

    def _record_count(self) -> int:
        return sum(map(len, (self.nodes, self.occurrences, self.assertions, self.evidence)))

    def check_work(self) -> None:
        self.check_stop()
        if self._record_count() >= self.record_limit:
            raise ValueError("project extraction record ceiling exceeded")

    def relation(self, source: CapturedSource, source_node: str, edge: str, target: str, start: int, end: int) -> None:
        assertion_id = _identifier(
            "assertion",
            f"{source.record.logical_id}:{source_node}:{edge}:{target}:{start}:{end}",
        )
        self.assertions[assertion_id] = {
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
        self.evidence[str(row["evidence_id"])] = row

    def add_source(self, source: object) -> None:
        if not isinstance(source, CapturedSource):
            raise TypeError("sources must contain CapturedSource values")
        self.check_work()
        path = source.record.relative_path
        if not path.endswith("/journal.md") or "/projects/" not in path:
            return
        self._add_journal(source, path.rsplit("/", 2)[-2])

    def _add_journal(self, source: CapturedSource, slug: str) -> None:
        project_id = _identifier("project", slug)
        self.nodes[project_id] = _node(project_id, "project", "project-slug/v1", slug)
        events = parse_journal_events(slug, source.content)
        search_start = 0
        for event in events:
            search_start = self._add_event(source, slug, project_id, event, search_start)

    def _add_event(
        self, source: CapturedSource, slug: str, project_id: str, event: Mapping[str, object], search_start: int
    ) -> int:
        """Project one event; the offset the next event is searched from."""
        self.check_work()
        event_start, event_end = _event_span(source.content, event, search_start)
        checkpoint_id = self._add_checkpoint(source, slug, event, event_start, event_end)
        self.relation(source, project_id, "PROJECT_HAS_CHECKPOINT", checkpoint_id, event_start, event_end)
        span = _EventSpan(source, slug, checkpoint_id, event_start, event_end)
        self._add_delta(span, event["delta"])
        self._add_evidence_events(span, event["evidence_event_ids"])
        if self._record_count() > self.record_limit:
            raise ValueError("project extraction record ceiling exceeded")
        return event_end

    def _add_checkpoint(
        self, source: CapturedSource, slug: str, event: Mapping[str, object], event_start: int, event_end: int
    ) -> str:
        checkpoint_id = _identifier("checkpoint", f"{slug}:{event['sequence']}")
        session = str(event["provenance"]["session"])
        session_id = _identifier("session", session)
        self.nodes[checkpoint_id] = _node(
            checkpoint_id, "checkpoint", "project-sequence/v1",
            f"{slug}:{event['sequence']}", sequence=event["sequence"],
        )
        self.nodes[session_id] = _node(session_id, "session", "session-id/v1", session)
        occurrence = _occurrence(source, checkpoint_id, "event", event_start, event_end)
        self.occurrences[str(occurrence["occurrence_id"])] = occurrence
        return checkpoint_id

    def _add_delta(self, span: _EventSpan, delta: object) -> None:
        assert isinstance(delta, Mapping)
        cursor = span.start
        for field, kind, edge in _DELTA_FAMILIES:
            operations = delta[field]
            assert isinstance(operations, list)
            cursor = self._add_operations(span, operations, kind, edge, cursor)

    def _add_operations(self, span: _EventSpan, operations: list, kind: str, edge: str, cursor: int) -> int:
        for operation in operations:
            self.check_work()
            if operation["action"] != "upsert":
                continue
            cursor = self._add_operation(span, operation, kind, edge, cursor)
        return cursor

    def _add_operation(self, span: _EventSpan, operation: Mapping, kind: str, edge: str, cursor: int) -> int:
        value = str(operation["value"])
        start, end = _literal_span(span, canonical_json_bytes(value), cursor)
        target_id = _identifier(kind, f"{span.slug}:{operation['id']}")
        self.nodes[target_id] = _node(
            target_id, kind, f"project-{kind}-id/v1", f"{span.slug}:{operation['id']}", value=value
        )
        self.relation(span.source, span.checkpoint_id, edge, target_id, start, end)
        return end

    def _add_evidence_events(self, span: _EventSpan, event_ids: list) -> None:
        for event_id in event_ids:
            self.check_work()
            token = canonical_json_bytes(event_id)
            start = span.source.content.find(token, span.start, span.end)
            if start < 0:
                raise ValueError("project evidence event ID is absent from event bytes")
            target_id = _identifier("event", str(event_id))
            self.nodes[target_id] = _node(target_id, "evidence", "event-id/v1", str(event_id))
            self.relation(
                span.source, span.checkpoint_id, "CHECKPOINT_EVIDENCED_BY_EVENT", target_id, start, start + len(token)
            )

    def result(self) -> ExtractionResult:
        return ExtractionResult(
            _ordered(self.nodes, "node_id"),
            _ordered(self.occurrences, "occurrence_id"),
            _ordered(self.assertions, "assertion_id"),
            _ordered(self.evidence, "evidence_id"),
            (),
        )
