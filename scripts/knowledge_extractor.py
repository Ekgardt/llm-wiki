"""Pure, bounded extraction of authoritative knowledge bytes into graph records."""

from __future__ import annotations

import hashlib
import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

import yaml
from corpus_snapshot import CapturedSource
from reliable_memory import canonical_json_bytes

EXTRACTOR_VERSION = "knowledge-extractor/v1"
MAX_SOURCES = 10_000
MAX_RECORDS = 100_000

_FRONTMATTER = re.compile(rb"\A---[ \t]*\r?\n(.*?)^---[ \t]*\r?\n", re.MULTILINE | re.DOTALL)
_WIKILINK = re.compile(rb"\[\[([^\]|#]+?)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
_CODE_SPAN = re.compile(rb"`([^`\r\n]+)`")
_BARE_SYMBOL = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/#@+\-]{0,511}")
# A backticked span longer than this is a quoted line, not a symbol. Session
# records hold verbatim conversation, and one of them carried a 20,000-character
# escaped-JSON line inside backticks: the extractor called it a symbol reference,
# the generation writer refused a target over 4096 characters, and the whole
# nightly build died on one quoted line.
MAX_SYMBOL_REFERENCE_CHARS = 512


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    nodes: tuple[dict[str, object], ...]
    occurrences: tuple[dict[str, object], ...]
    assertions: tuple[dict[str, object], ...]
    evidence: tuple[dict[str, object], ...]
    observations: tuple[dict[str, object], ...]
    dependencies: tuple[dict[str, object], ...] = ()


def _identifier(prefix: str, value: str) -> str:
    candidate = f"{prefix}:{value}"
    if _SAFE_ID.fullmatch(candidate):
        return candidate
    return f"{prefix}:sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def _finite_timestamp(deadline: object) -> bool:
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        return False
    return math.isfinite(deadline)


def _check_deadline(deadline: float | None, monotonic: Callable[[], float]) -> None:
    if deadline is None:
        return
    if not _finite_timestamp(deadline):
        raise ValueError("deadline must be a finite monotonic timestamp")
    if monotonic() >= deadline:
        raise TimeoutError("knowledge extraction deadline reached")


def _check_stop(
    deadline: float | None,
    monotonic: Callable[[], float],
    cancelled: Callable[[], bool] | None,
) -> None:
    _check_deadline(deadline, monotonic)
    if cancelled is not None and cancelled():
        raise TimeoutError("knowledge extraction cancelled")


def _span(source: CapturedSource, start: int, end: int) -> dict[str, object]:
    return {
        "source_id": source.record.logical_id,
        "byte_start": start,
        "byte_end": end,
        "span_sha256": hashlib.sha256(source.content[start:end]).hexdigest(),
    }


def _evidence(
    source: CapturedSource,
    record_id: str,
    start: int,
    end: int,
    *,
    observation: bool = False,
) -> dict[str, object]:
    return {
        "evidence_id": _identifier("evidence", record_id),
        "assertion_id": None if observation else record_id,
        "observation_id": record_id if observation else None,
        **_span(source, start, end),
    }


def _node(node_id: str, kind: str, scheme: str, key: str, **metadata: object) -> dict[str, object]:
    return {
        "node_id": node_id,
        "kind": kind,
        "identity_scheme": scheme,
        "identity_key": key,
        "metadata": metadata,
    }


def _occurrence(source: CapturedSource, node_id: str, role: str, start: int, end: int) -> dict[str, object]:
    return {
        "occurrence_id": _identifier("occurrence", f"{source.record.logical_id}:{node_id}:{start}:{end}"),
        "node_id": node_id,
        "source_id": source.record.logical_id,
        "role": role,
        "byte_start": start,
        "byte_end": end,
        "line_start": source.content.count(b"\n", 0, start) + 1,
        "line_end": source.content.count(b"\n", 0, end) + 1,
    }


def _frontmatter(source: CapturedSource) -> tuple[dict[str, object], re.Match[bytes] | None]:
    match = _FRONTMATTER.search(source.content)
    if match is None:
        return {}, None
    try:
        value = yaml.safe_load(match.group(1).decode("utf-8", errors="strict")) or {}
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValueError(f"invalid knowledge frontmatter: {source.record.relative_path}") from exc
    if not isinstance(value, dict):
        raise ValueError("knowledge frontmatter must be a mapping")
    return value, match


def _field_span(source: CapturedSource, field: str, fallback: tuple[int, int]) -> tuple[int, int]:
    match = re.search(
        rb"(?m)^" + re.escape(field.encode()) + rb":[ \t]*([^\r\n]+)", source.content
    )
    return fallback if match is None else match.span(1)


def _page_kind(page_type: object) -> str:
    if page_type == "decision":
        return "decision"
    if page_type == "debugging":
        return "debugging-note"
    return "knowledge-page"


def _is_explicit_symbol_reference(reference: str) -> bool:
    return reference.startswith(("scip-", "symbol:")) or (
        "/" in reference and ("::" in reference or ":" in reference or "#" in reference)
    )


def _assertion(record_id: str, source: str, edge: str, target: str, authority: str) -> dict[str, object]:
    return {
        "assertion_id": record_id,
        "source_node_id": source,
        "edge_type": edge,
        "target_node_id": target,
        "literal": None,
        "confidence": "high",
        "authority": authority if authority in {"user", "web", "ai-derived", "inferred"} else "inferred",
        "resolution": "resolved",
        "extractor": EXTRACTOR_VERSION,
    }


class _Extraction:
    """One extraction pass: the rows it has built, and the work that builds them."""

    def __init__(
        self,
        ordered: Sequence[CapturedSource],
        symbols: Mapping[str, str],
        *,
        record_limit: int,
        deadline: float | None,
        monotonic: Callable[[], float],
        cancelled: Callable[[], bool] | None,
    ) -> None:
        self.ordered = ordered
        self.symbols = dict(symbols)
        self.record_limit = record_limit
        self.deadline = deadline
        self.monotonic = monotonic
        self.cancelled = cancelled
        self.nodes: dict[str, dict[str, object]] = {}
        self.occurrences: dict[str, dict[str, object]] = {}
        self.assertions: dict[str, dict[str, object]] = {}
        self.evidence: dict[str, dict[str, object]] = {}
        self.observations: dict[str, dict[str, object]] = {}
        self.metadata_by_path: dict[str, dict[str, object]] = {}
        self.page_by_path: dict[str, str] = {}
        self.aliases: dict[str, list[str]] = {}
        self._index_pages()

    # --- bookkeeping ------------------------------------------------------

    def _index_pages(self) -> None:
        for source in self.ordered:
            _check_stop(self.deadline, self.monotonic, self.cancelled)
            metadata, _match = _frontmatter(source)
            path = source.record.relative_path
            self.metadata_by_path[path] = metadata
            self.page_by_path[path] = _identifier("page", path)
            for alias in _aliases_of(path):
                self.aliases.setdefault(alias, []).append(path)

    def _count(self) -> int:
        return sum(
            map(
                len,
                (
                    self.nodes,
                    self.occurrences,
                    self.assertions,
                    self.evidence,
                    self.observations,
                ),
            )
        )

    def check_work(self) -> None:
        _check_stop(self.deadline, self.monotonic, self.cancelled)
        if self._count() >= self.record_limit:
            raise ValueError("knowledge extraction record ceiling exceeded")

    def _require_room(self) -> None:
        if self._count() > self.record_limit:
            raise ValueError("knowledge extraction record ceiling exceeded")

    def add_relation(
        self,
        source: CapturedSource,
        source_node: str,
        edge: str,
        target_node: str,
        start: int,
        end: int,
        authority: str,
    ) -> None:
        key = f"{source.record.logical_id}:{source_node}:{edge}:{target_node}:{start}:{end}"
        record_id = _identifier("assertion", key)
        self.assertions[record_id] = _assertion(
            record_id, source_node, edge, target_node, authority
        )
        row = _evidence(source, record_id, start, end)
        self.evidence[str(row["evidence_id"])] = row

    def add_observation(
        self,
        source: CapturedSource,
        source_node: str,
        edge: str,
        target: str,
        reason: str,
        start: int,
        end: int,
    ) -> None:
        key = (
            f"{source.record.logical_id}:{source_node}:{edge}:{target}:{reason}"
            f":{start}:{end}"
        )
        record_id = _identifier("observation", key)
        self.observations[record_id] = {
            "observation_id": record_id,
            "source_node_id": source_node,
            "edge_type": edge,
            "target_text": target,
            "reason": reason,
            "extractor": EXTRACTOR_VERSION,
        }
        row = _evidence(source, record_id, start, end, observation=True)
        self.evidence[str(row["evidence_id"])] = row

    def _resolve(self, target: str) -> list[str]:
        return sorted(dict.fromkeys(self.aliases.get(target.casefold(), [])))

    def _link(
        self,
        source: CapturedSource,
        page_id: str,
        edge: str,
        target: str,
        span: tuple[int, int],
        authority: str,
    ) -> None:
        """One reference: an edge when it resolves to exactly one page, else a note."""
        candidates = self._resolve(target)
        if len(candidates) == 1:
            self.add_relation(
                source,
                page_id,
                edge,
                self.page_by_path[candidates[0]],
                span[0],
                span[1],
                authority,
            )
            return
        self.add_observation(
            source,
            page_id,
            edge,
            target,
            "ambiguous_target" if candidates else "unresolved_reference",
            span[0],
            span[1],
        )

    # --- per-source extraction -------------------------------------------

    def add_source(self, source: CapturedSource) -> None:
        self.check_work()
        path = source.record.relative_path
        metadata = self.metadata_by_path[path]
        page_id = self.page_by_path[path]
        authority = str(
            metadata.get("source_authority") or metadata.get("authority") or "inferred"
        )
        self._page_node(source, page_id, metadata)
        self._project_edge(source, page_id, metadata, authority)
        self._wikilinks(source, page_id, authority)
        self._supersession(source, page_id, metadata, authority)
        self._code_spans(source, page_id, authority)
        self._claims(source, page_id, authority)
        self._require_room()

    def _page_node(
        self, source: CapturedSource, page_id: str, metadata: Mapping[str, object]
    ) -> None:
        self.nodes[page_id] = _node(
            page_id,
            _page_kind(metadata.get("type") or source.metadata.type),
            "knowledge-path/v1",
            source.record.relative_path,
            page_type=metadata.get("type") or source.metadata.type,
            status=metadata.get("status") or "active",
        )
        if not source.content:
            return
        occurrence = _occurrence(source, page_id, "definition", 0, len(source.content))
        self.occurrences[str(occurrence["occurrence_id"])] = occurrence

    def _project_edge(
        self,
        source: CapturedSource,
        page_id: str,
        metadata: Mapping[str, object],
        authority: str,
    ) -> None:
        project = metadata.get("project") or source.metadata.project
        if not isinstance(project, str) or not project:
            return
        project_id = _identifier("project", project)
        self.nodes[project_id] = _node(
            project_id, "project", "project-slug/v1", project
        )
        start, end = _field_span(source, "project", (0, min(1, len(source.content))))
        self.add_relation(
            source, page_id, "BELONGS_TO_PROJECT", project_id, start, end, authority
        )

    def _wikilinks(
        self, source: CapturedSource, page_id: str, authority: str
    ) -> None:
        for match in _WIKILINK.finditer(source.content):
            self.check_work()
            target = match.group(1).decode("utf-8", errors="strict").strip()
            self._link(
                source,
                page_id,
                "LINKS_TO",
                target.removesuffix(".md"),
                (match.start(), match.end()),
                authority,
            )

    def _supersession(
        self,
        source: CapturedSource,
        page_id: str,
        metadata: Mapping[str, object],
        authority: str,
    ) -> None:
        superseded_by = metadata.get("superseded_by")
        if not isinstance(superseded_by, str):
            return
        target = superseded_by.strip().removeprefix("[[").removesuffix("]]")
        span = _field_span(source, "superseded_by", (0, min(1, len(source.content))))
        candidates = self._resolve(target)
        if len(candidates) == 1:
            self.add_relation(
                source,
                self.page_by_path[candidates[0]],
                "SUPERSEDES",
                page_id,
                span[0],
                span[1],
                authority,
            )
            return
        self.add_observation(
            source,
            page_id,
            "SUPERSEDES",
            target,
            "ambiguous_target" if candidates else "unresolved_reference",
            span[0],
            span[1],
        )

    def _code_spans(
        self, source: CapturedSource, page_id: str, authority: str
    ) -> None:
        for match in _CODE_SPAN.finditer(source.content):
            self.check_work()
            reference = match.group(1).decode("utf-8", errors="strict")
            if len(reference) > MAX_SYMBOL_REFERENCE_CHARS:
                continue
            self._code_span(source, page_id, reference, match, authority)

    def _code_span(
        self,
        source: CapturedSource,
        page_id: str,
        reference: str,
        match: object,
        authority: str,
    ) -> None:
        target_node = self.symbols.get(reference)
        if target_node is None:
            self._unresolved_symbol(source, page_id, reference, match)
            return
        if not _is_explicit_symbol_reference(reference):
            self.add_observation(
                source,
                page_id,
                "REFERENCES_SYMBOL",
                reference,
                "ambiguous_target",
                match.start(),
                match.end(),
            )
            return
        self.nodes.setdefault(
            target_node, _node(target_node, "symbol", "external-symbol/v1", reference)
        )
        self.add_relation(
            source,
            page_id,
            "REFERENCES_SYMBOL",
            target_node,
            match.start(),
            match.end(),
            authority,
        )

    def _unresolved_symbol(
        self, source: CapturedSource, page_id: str, reference: str, match: object
    ) -> None:
        if not _BARE_SYMBOL.fullmatch(reference) and not _is_explicit_symbol_reference(
            reference
        ):
            return
        self.add_observation(
            source,
            page_id,
            "REFERENCES_SYMBOL",
            reference,
            "ambiguous_target",
            match.start(),
            match.end(),
        )

    def _claims(self, source: CapturedSource, page_id: str, authority: str) -> None:
        from claims import parse_claim_ledger

        ledger = parse_claim_ledger(source.content)
        if ledger is None:
            return
        search_start = 0
        for record in ledger["claims"]:
            self.check_work()
            search_start = self._claim(source, record, search_start, authority)

    def _claim(
        self,
        source: CapturedSource,
        record: Mapping[str, object],
        search_start: int,
        authority: str,
    ) -> int:
        encoded = canonical_json_bytes(record)
        start = source.content.find(encoded, search_start)
        if start < 0:
            raise ValueError("canonical claim record is not present in source bytes")
        end = start + len(encoded)
        claim_id = _identifier("claim", str(record["id"]))
        reference = str(record["evidence"]["reference"])
        evidence_node = _identifier("evidence-node", reference)
        self.nodes[claim_id] = _node(claim_id, "claim", "claim-id/v1", str(record["id"]))
        self.nodes[evidence_node] = _node(
            evidence_node, "evidence", "evidence-reference/v1", reference
        )
        occurrence = _occurrence(source, claim_id, "definition", start, end)
        self.occurrences[str(occurrence["occurrence_id"])] = occurrence
        self.add_relation(
            source, claim_id, "EVIDENCED_BY", evidence_node, start, end, authority
        )
        return end

    def result(self) -> ExtractionResult:
        return ExtractionResult(
            _ordered_rows(self.nodes, "node_id"),
            _ordered_rows(self.occurrences, "occurrence_id"),
            _ordered_rows(self.assertions, "assertion_id"),
            _ordered_rows(self.evidence, "evidence_id"),
            _ordered_rows(self.observations, "observation_id"),
        )


def _aliases_of(path: str) -> set[str]:
    names = {path, path.removesuffix(".md"), path.rsplit("/", 1)[-1].removesuffix(".md")}
    return {alias.casefold() for alias in names}


def _ordered_rows(
    rows: Mapping[str, dict[str, object]], key: str
) -> tuple[dict[str, object], ...]:
    return tuple(sorted(rows.values(), key=lambda row: str(row[key])))


def _require_positive_bound(value: object, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be positive")


def _require_source_sequence(sources: object) -> None:
    if isinstance(sources, (bytes, str)) or not isinstance(sources, Sequence):
        raise TypeError("sources must be a sequence of CapturedSource values")


def _require_callable_cancel(cancelled: object) -> None:
    if cancelled is not None and not callable(cancelled):
        raise TypeError("cancelled must be callable")


def _require_extraction_arguments(
    sources: object, max_sources: object, max_records: object, cancelled: object
) -> None:
    _require_source_sequence(sources)
    _require_positive_bound(max_sources, "max_sources")
    _require_positive_bound(max_records, "max_records")
    _require_callable_cancel(cancelled)
    assert isinstance(max_sources, int) and isinstance(sources, Sequence)
    if len(sources) > min(max_sources, MAX_SOURCES):
        raise ValueError("knowledge extraction source ceiling exceeded")


def _require_captured_sources(ordered: Sequence[object]) -> None:
    if any(not isinstance(item, CapturedSource) for item in ordered):
        raise TypeError("sources must contain CapturedSource values")


def _require_unique_sources(ordered: Sequence[CapturedSource]) -> None:
    paths = [item.record.relative_path for item in ordered]
    source_ids = [item.record.logical_id for item in ordered]
    if len(paths) != len(set(paths)) or len(source_ids) != len(set(source_ids)):
        raise ValueError("captured sources must have unique paths and logical IDs")


def _require_intact_bytes(ordered: Sequence[CapturedSource]) -> None:
    for item in ordered:
        if item.record.size != len(item.content) or item.record.sha256 != hashlib.sha256(
            item.content
        ).hexdigest():
            raise ValueError("captured source bytes do not match immutable metadata")


def extract_knowledge(
    sources: Sequence[CapturedSource],
    *,
    symbol_index: Mapping[str, str] | None = None,
    max_sources: int = MAX_SOURCES,
    max_records: int = MAX_RECORDS,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    cancelled: Callable[[], bool] | None = None,
) -> ExtractionResult:
    """Extract deterministic graph rows without reading or mutating live files."""
    _require_extraction_arguments(sources, max_sources, max_records, cancelled)
    _check_stop(deadline, monotonic, cancelled)
    ordered = sorted(sources, key=lambda item: item.record.relative_path)
    _require_captured_sources(ordered)
    _require_unique_sources(ordered)
    _require_intact_bytes(ordered)
    extraction = _Extraction(
        ordered,
        symbol_index or {},
        record_limit=min(max_records, MAX_RECORDS),
        deadline=deadline,
        monotonic=monotonic,
        cancelled=cancelled,
    )
    for source in ordered:
        extraction.add_source(source)
    return extraction.result()
