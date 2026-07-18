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
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/#@+\-]{0,511}")


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


def _check_deadline(deadline: float | None, monotonic: Callable[[], float]) -> None:
    if deadline is not None and (
        isinstance(deadline, bool)
        or not isinstance(deadline, (int, float))
        or not math.isfinite(deadline)
    ):
        raise ValueError("deadline must be a finite monotonic timestamp")
    if deadline is not None and monotonic() >= deadline:
        raise TimeoutError("knowledge extraction deadline reached")


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


def extract_knowledge(
    sources: Sequence[CapturedSource],
    *,
    symbol_index: Mapping[str, str] | None = None,
    max_sources: int = MAX_SOURCES,
    max_records: int = MAX_RECORDS,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> ExtractionResult:
    """Extract deterministic graph rows without reading or mutating live files."""
    from claims import parse_claim_ledger

    if isinstance(sources, (bytes, str)) or not isinstance(sources, Sequence):
        raise TypeError("sources must be a sequence of CapturedSource values")
    if not isinstance(max_sources, int) or isinstance(max_sources, bool) or max_sources < 1:
        raise ValueError("max_sources must be positive")
    if not isinstance(max_records, int) or isinstance(max_records, bool) or max_records < 1:
        raise ValueError("max_records must be positive")
    if len(sources) > min(max_sources, MAX_SOURCES):
        raise ValueError("knowledge extraction source ceiling exceeded")
    _check_deadline(deadline, monotonic)
    ordered = sorted(sources, key=lambda item: item.record.relative_path)
    if any(not isinstance(item, CapturedSource) for item in ordered):
        raise TypeError("sources must contain CapturedSource values")
    paths = [item.record.relative_path for item in ordered]
    source_ids = [item.record.logical_id for item in ordered]
    if len(paths) != len(set(paths)) or len(source_ids) != len(set(source_ids)):
        raise ValueError("captured sources must have unique paths and logical IDs")
    for item in ordered:
        if (
            item.record.size != len(item.content)
            or item.record.sha256 != hashlib.sha256(item.content).hexdigest()
        ):
            raise ValueError("captured source bytes do not match immutable metadata")
    symbols = dict(symbol_index or {})
    record_limit = min(max_records, MAX_RECORDS)

    metadata_by_path: dict[str, dict[str, object]] = {}
    page_by_path: dict[str, str] = {}
    aliases: dict[str, list[str]] = {}
    for source in ordered:
        _check_deadline(deadline, monotonic)
        metadata, _match = _frontmatter(source)
        path = source.record.relative_path
        page_id = _identifier("page", path)
        metadata_by_path[path] = metadata
        page_by_path[path] = page_id
        for alias in {path, path.removesuffix(".md"), path.rsplit("/", 1)[-1].removesuffix(".md")}:
            aliases.setdefault(alias.casefold(), []).append(path)

    nodes: dict[str, dict[str, object]] = {}
    occurrences: dict[str, dict[str, object]] = {}
    assertions: dict[str, dict[str, object]] = {}
    evidence: dict[str, dict[str, object]] = {}
    observations: dict[str, dict[str, object]] = {}

    def add_relation(
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
        assertions[record_id] = _assertion(record_id, source_node, edge, target_node, authority)
        row = _evidence(source, record_id, start, end)
        evidence[str(row["evidence_id"])] = row

    def add_observation(
        source: CapturedSource,
        source_node: str,
        edge: str,
        target: str,
        reason: str,
        start: int,
        end: int,
    ) -> None:
        key = f"{source.record.logical_id}:{source_node}:{edge}:{target}:{reason}:{start}:{end}"
        record_id = _identifier("observation", key)
        observations[record_id] = {
            "observation_id": record_id,
            "source_node_id": source_node,
            "edge_type": edge,
            "target_text": target,
            "reason": reason,
            "extractor": EXTRACTOR_VERSION,
        }
        row = _evidence(source, record_id, start, end, observation=True)
        evidence[str(row["evidence_id"])] = row

    for source in ordered:
        _check_deadline(deadline, monotonic)
        path = source.record.relative_path
        metadata = metadata_by_path[path]
        page_id = page_by_path[path]
        authority = str(metadata.get("source_authority") or metadata.get("authority") or "inferred")
        nodes[page_id] = _node(
            page_id,
            _page_kind(metadata.get("type") or source.metadata.type),
            "knowledge-path/v1",
            path,
            page_type=metadata.get("type") or source.metadata.type,
            status=metadata.get("status") or "active",
        )
        if source.content:
            occurrence = _occurrence(source, page_id, "definition", 0, len(source.content))
            occurrences[str(occurrence["occurrence_id"])] = occurrence

        project = metadata.get("project") or source.metadata.project
        if isinstance(project, str) and project:
            project_id = _identifier("project", project)
            nodes[project_id] = _node(project_id, "project", "project-slug/v1", project)
            start, end = _field_span(source, "project", (0, min(1, len(source.content))))
            add_relation(source, page_id, "BELONGS_TO_PROJECT", project_id, start, end, authority)

        for match in _WIKILINK.finditer(source.content):
            target = match.group(1).decode("utf-8", errors="strict").strip()
            candidates = sorted(dict.fromkeys(aliases.get(target.removesuffix(".md").casefold(), [])))
            if len(candidates) == 1:
                add_relation(
                    source, page_id, "LINKS_TO", page_by_path[candidates[0]],
                    match.start(), match.end(), authority,
                )
            else:
                add_observation(
                    source, page_id, "LINKS_TO", target,
                    "ambiguous_target" if candidates else "unresolved_reference",
                    match.start(), match.end(),
                )

        superseded_by = metadata.get("superseded_by")
        if isinstance(superseded_by, str):
            target = superseded_by.strip().removeprefix("[[").removesuffix("]]")
            candidates = sorted(dict.fromkeys(aliases.get(target.casefold(), [])))
            start, end = _field_span(source, "superseded_by", (0, min(1, len(source.content))))
            if len(candidates) == 1:
                add_relation(
                    source, page_by_path[candidates[0]], "SUPERSEDES", page_id,
                    start, end, authority,
                )
            else:
                add_observation(source, page_id, "SUPERSEDES", target, "ambiguous_target" if candidates else "unresolved_reference", start, end)

        for match in _CODE_SPAN.finditer(source.content):
            reference = match.group(1).decode("utf-8", errors="strict")
            target_node = symbols.get(reference)
            if target_node is None:
                continue
            explicit = (
                reference.startswith(("scip-", "symbol:"))
                or ("/" in reference and ("::" in reference or ":" in reference or "#" in reference))
            )
            if explicit:
                nodes.setdefault(target_node, _node(target_node, "symbol", "external-symbol/v1", reference))
                add_relation(source, page_id, "REFERENCES_SYMBOL", target_node, match.start(), match.end(), authority)
            else:
                add_observation(source, page_id, "REFERENCES_SYMBOL", reference, "ambiguous_target", match.start(), match.end())

        ledger = parse_claim_ledger(source.content)
        if ledger is not None:
            search_start = 0
            for record in ledger["claims"]:
                encoded = canonical_json_bytes(record)
                start = source.content.find(encoded, search_start)
                if start < 0:
                    raise ValueError("canonical claim record is not present in source bytes")
                end = start + len(encoded)
                search_start = end
                claim_id = _identifier("claim", str(record["id"]))
                reference = str(record["evidence"]["reference"])
                evidence_node = _identifier("evidence-node", reference)
                nodes[claim_id] = _node(claim_id, "claim", "claim-id/v1", str(record["id"]))
                nodes[evidence_node] = _node(evidence_node, "evidence", "evidence-reference/v1", reference)
                occurrence = _occurrence(source, claim_id, "definition", start, end)
                occurrences[str(occurrence["occurrence_id"])] = occurrence
                add_relation(source, claim_id, "EVIDENCED_BY", evidence_node, start, end, authority)

        if sum(map(len, (nodes, occurrences, assertions, evidence, observations))) > record_limit:
            raise ValueError("knowledge extraction record ceiling exceeded")

    def order(
        rows: Mapping[str, dict[str, object]], key: str
    ) -> tuple[dict[str, object], ...]:
        return tuple(sorted(rows.values(), key=lambda row: str(row[key])))

    return ExtractionResult(
        order(nodes, "node_id"),
        order(occurrences, "occurrence_id"),
        order(assertions, "assertion_id"),
        order(evidence, "evidence_id"),
        order(observations, "observation_id"),
    )
