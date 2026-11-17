"""Navigation requests, exact direct-result checks, citations and token records for the qualification."""

from __future__ import annotations

import hashlib
import re
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

SCRIPTS_ROOT = Path(__file__).resolve().parent.parent / "scripts"
BENCHMARK_ROOT = Path(__file__).resolve().parent
for _path in (SCRIPTS_ROOT, BENCHMARK_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from code_intelligence import Capability, PositionEncoding  # noqa: E402
from code_navigation import (  # noqa: E402
    NavigationLocation,
    NavigationRequest,
    NavigationResult,
    NavigationStatus,
)
from code_navigation_renderer import estimate_tokens  # noqa: E402
from generate_python_qualification import (  # noqa: E402
    GoldLocation,
    GoldQuery,
    QualificationRepository,
)
from lsp_positions import SourceDocument  # noqa: E402
from lsp_security import (  # noqa: E402
    normalize_provider_uri,
    read_repository_source_bytes,
)
from navigation_benchmark_schema import _canonical_json  # noqa: E402
from pyright_session import LspLocation  # noqa: E402
from repository_scope import RepositoryScope  # noqa: E402

DEFAULT_LIMIT = 10
_DIRECT_VALIDATION_MAX_SOURCE_BYTES = 16 * 1024 * 1024
_HEX_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _capability(value: str) -> Capability:
    return {
        "definition": Capability.DEFINITIONS,
        "references": Capability.REFERENCES,
        "calls": Capability.CALLS,
    }[value]


def _navigation_request(query: GoldQuery, scope: RepositoryScope) -> NavigationRequest:
    return NavigationRequest(
        scope,
        _capability(query.capability),
        query.path,
        query.line,
        query.character,
        0,
        DEFAULT_LIMIT,
        query.direction,
    )


def _expected_key(location: GoldLocation) -> tuple[object, ...]:
    return (
        location.path,
        location.line,
        location.character,
        location.byte_start,
        location.byte_end,
    )


def _actual_key(location: NavigationLocation) -> tuple[object, ...]:
    return (
        location.path,
        location.line,
        location.character,
        location.range.byte_start,
        location.range.byte_end,
    )


def _navigation_assertion_succeeds(
    result: NavigationResult,
    *,
    expected: set[tuple[object, ...]],
    actual: set[tuple[object, ...]],
    citations_current: bool,
) -> bool:
    accepted_statuses = (
        {NavigationStatus.OK, NavigationStatus.PARTIAL}
        if expected
        else {NavigationStatus.OK}
    )
    return result.status in accepted_statuses and actual == expected and citations_current


def _expected_hashes(query: GoldQuery) -> dict[tuple[object, ...], str]:
    return {_expected_key(location): location.source_sha256 for location in query.expected_locations}


def _direct_location_key(
    scope: RepositoryScope, location: object, encoding: PositionEncoding, deadline: float
) -> tuple[tuple[object, ...], str] | None:
    """(key, source sha256) for one provider location; None when it cannot be resolved."""
    if time.monotonic() >= deadline or not isinstance(location, LspLocation):
        return None
    source = normalize_provider_uri(scope, location.uri)
    if source is None:
        return None
    try:
        content = read_repository_source_bytes(
            scope,
            source.relative_path,
            max_bytes=_DIRECT_VALIDATION_MAX_SOURCE_BYTES,
            deadline=deadline,
        )
        document = SourceDocument.from_bytes(source.relative_path, content)
        range_ = document.to_byte_range(location.range, encoding)
        line_start, _line_end = document.line_spans[location.range.start.line]
    except Exception:
        return None
    key = (
        source.relative_path,
        location.range.start.line + 1,
        range_.byte_start - line_start,
        range_.byte_start,
        range_.byte_end,
    )
    return key, document.source_sha256


def _resolved_matches(resolved: tuple[tuple[object, ...], str] | None, expected_hashes: dict) -> bool:
    return resolved is not None and resolved[1] == expected_hashes.get(resolved[0])


def _provider_locations(result: object) -> tuple | None:
    """The locations of a provider-reported result, or None for any other result."""
    if getattr(result, "coverage", None) != "provider_reported":
        return None
    locations = getattr(result, "locations", None)
    if not isinstance(locations, tuple):
        return None
    return locations


def _direct_result_keys(
    scope: RepositoryScope, result: object, encoding: PositionEncoding, expected_hashes: dict, deadline: float
) -> set[tuple[object, ...]] | None:
    locations = _provider_locations(result)
    if locations is None:
        return None
    actual: set[tuple[object, ...]] = set()
    for location in locations:
        resolved = _direct_location_key(scope, location, encoding, deadline)
        if not _resolved_matches(resolved, expected_hashes):
            return None
        actual.add(resolved[0])
    return actual


def _direct_results_are_exact(
    runtime: object,
    repository: QualificationRepository,
    scope: RepositoryScope,
    query: GoldQuery,
    results: Sequence[object],
    *,
    deadline: float,
) -> bool:
    encoding = getattr(runtime, "position_encoding", None)
    if not isinstance(encoding, PositionEncoding):
        return False
    expected = {_expected_key(location) for location in query.expected_locations}
    expected_hashes = _expected_hashes(query)
    for result in results:
        if _direct_result_keys(scope, result, encoding, expected_hashes, deadline) != expected:
            return False
    return True


def _cited_content(repository_root: Path, relative: str) -> bytes:
    root = repository_root.resolve(strict=True)
    path = (root / relative).resolve(strict=True)
    path.relative_to(root)
    return path.read_bytes()


def _citation_span_current(content: bytes, location: NavigationLocation) -> bool:
    start = location.range.byte_start
    end = location.range.byte_end
    if not 0 <= start < end <= len(content):
        return False
    content[:start].decode("utf-8", errors="strict")
    content[start:end].decode("utf-8", errors="strict")
    line_start = content.rfind(b"\n", 0, start) + 1
    return content.count(b"\n", 0, start) + 1 == location.line and start - line_start == location.character


def _current_citation(
    repository_root: Path,
    location: NavigationLocation,
    *,
    expected_sha256: str,
) -> bool:
    try:
        if _HEX_SHA256.fullmatch(expected_sha256) is None:
            return False
        content = _cited_content(repository_root, location.path)
        if hashlib.sha256(content).hexdigest() != expected_sha256:
            return False
        return _citation_span_current(content, location)
    except (OSError, UnicodeError, ValueError):
        return False


def _citations_current(repository_root: Path, locations, expected_hashes: dict) -> bool:
    return all(
        _current_citation(repository_root, location, expected_sha256=expected_hashes.get(_actual_key(location), ""))
        for location in locations
    )


def _citation_flags(repository_root: Path, query: GoldQuery, result: NavigationResult) -> list[bool]:
    expected_hashes = _expected_hashes(query)
    return [
        _current_citation(repository_root, location, expected_sha256=expected_hashes.get(_actual_key(location), ""))
        for location in result.locations
    ]


def _group_location_count(group: object) -> int:
    if isinstance(group, dict) and isinstance(group.get("locations"), list):
        return len(group["locations"])
    return 0


def _rendered_item_count(payload: Mapping[str, object]) -> int:
    groups = payload.get("groups", [])
    diagnostics = payload.get("diagnostics", [])
    locations = sum(_group_location_count(group) for group in groups) if isinstance(groups, list) else 0
    return locations + (len(diagnostics) if isinstance(diagnostics, list) else 0)


def _request_value(request: NavigationRequest) -> dict[str, object]:
    return {
        "capability": request.capability.value,
        "path": request.path,
        "line": request.line,
        "character": request.character,
        "direction": request.direction,
        "offset": request.offset,
        "limit": request.limit,
    }


def _provenance_value(item: object) -> dict[str, str]:
    return {
        "source": item.source,
        "provider": item.provider,
        "version": item.version,
        "observation": item.observation,
    }


def _location_value(location: NavigationLocation) -> dict[str, object]:
    return {
        "path": location.path,
        "range": {"byte_start": location.range.byte_start, "byte_end": location.range.byte_end},
        "line": location.line,
        "character": location.character,
        "containing_symbol": location.containing_symbol,
        "signature": location.signature,
        "resolution": location.resolution.value,
        "provenance": [_provenance_value(item) for item in location.provenance],
    }


def _diagnostic_value(diagnostic: object) -> dict[str, object]:
    return {
        "path": diagnostic.path,
        "range": {"byte_start": diagnostic.range.byte_start, "byte_end": diagnostic.range.byte_end},
        "severity": diagnostic.severity.value,
        "code": diagnostic.code,
        "message": diagnostic.message,
        "related": [_location_value(location) for location in diagnostic.related],
        "provenance": [_provenance_value(item) for item in diagnostic.provenance],
    }


def _enum_value_or_none(value: object) -> object:
    if value is None:
        return None
    return value.value


def _raw_result_value(result: NavigationResult) -> dict[str, object]:
    return {
        "status": result.status.value,
        "requested_capability": result.requested_capability.value,
        "effective_capability": _enum_value_or_none(result.effective_capability),
        "provider": result.provider,
        "provider_version": result.provider_version,
        "repository_id": result.repository_id,
        "checkout_id": result.checkout_id,
        "workspace_revision_before": result.workspace_revision_before,
        "workspace_revision_after": result.workspace_revision_after,
        "document_version": result.document_version,
        "position_encoding": _enum_value_or_none(result.position_encoding),
        "readiness": result.readiness,
        "symbol": result.symbol,
        "total": result.total,
        "offset": result.offset,
        "limit": result.limit,
        "locations": [_location_value(location) for location in result.locations],
        "diagnostics": [_diagnostic_value(diagnostic) for diagnostic in result.diagnostics],
        "hover": result.hover,
        "resolution": result.resolution.value,
        "provenance": [_provenance_value(item) for item in result.provenance],
        "warnings": list(result.warnings),
    }


def _token_record(
    query: GoldQuery,
    request: NavigationRequest,
    result: NavigationResult,
    rendered: Mapping[str, object],
) -> dict[str, object]:
    return {
        "query_id": query.query_id,
        "uncached_input_tokens": estimate_tokens(_canonical_json(_request_value(request))),
        "cache_read_tokens": 0,
        "raw_tool_tokens": estimate_tokens(_canonical_json(_raw_result_value(result))),
        "output_tokens": estimate_tokens(_canonical_json(rendered)),
    }


def _query_position(content: bytes, byte_start: int) -> tuple[int, int, int]:
    line_start = content.rfind(b"\n", 0, byte_start) + 1
    prefix = content[line_start:byte_start]
    return (
        content.count(b"\n", 0, byte_start) + 1,
        len(prefix),
        len(prefix.decode("utf-8", errors="strict")),
    )
