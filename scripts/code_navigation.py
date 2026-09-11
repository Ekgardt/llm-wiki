"""Normalized, freshness-proven, capability-honest code navigation facade."""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from enum import Enum
from types import MappingProxyType
from typing import NoReturn, TypeVar

from bounded_io import read_stable_bytes
from code_intelligence import (
    Capability,
    DiagnosticSeverity,
    PositionEncoding,
    PositionRange,
)
from lsp_positions import SourceAnchor, SourceDocument
from lsp_security import (
    RepositorySource,
    normalize_provider_uri,
    resolve_repository_source,
    validate_repository_relative_path,
)
from pyright_profile import PyrightIdentity
from pyright_session import (
    LspDiagnostic,
    LspLocation,
    OpenDocument,
    ProviderDiagnostics,
    ProviderHover,
    ProviderLocations,
    PyrightSession,
)
from repository_scope import RepositoryScope
from workspace_revision import (
    RevisionEntry,
    WorkspaceRevision,
    compute_workspace_revision,
    verify_workspace_revision_unchanged,
)


class NavigationStatus(str, Enum):
    OK = "ok"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"
    NOT_READY = "not_ready"
    STALE = "stale"
    TIMEOUT = "timeout"
    ERROR = "error"


class ResolutionLabel(str, Enum):
    LSP_CONFIRMED = "lsp_confirmed"
    GRAPH_CONFIRMED = "graph_confirmed"
    LSP_AND_GRAPH = "lsp_and_graph"
    LSP_ONLY = "lsp_only"
    GRAPH_CANDIDATE = "graph_candidate"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"
    UNSUPPORTED = "unsupported"


_CAPABILITY_DIRECTION: Mapping[Capability, str | None] = MappingProxyType(
    {
        Capability.DEFINITIONS: None,
        Capability.REFERENCES: None,
        Capability.IMPLEMENTATIONS: None,
        Capability.TYPE_DEFINITIONS: None,
        Capability.TYPES: None,
        Capability.DIAGNOSTICS: None,
        Capability.CALLS: "incoming",
    }
)

_MAX_NAVIGATION_FACTS = 10_000
_MAX_NAVIGATION_INPUT_VALUES = 100_000
_MAX_NAVIGATION_SOURCE_BYTES = 16 * 1024 * 1024
_MAX_SOURCE_DOCUMENT_CACHE_ENTRIES = 128
_MAX_SOURCE_DOCUMENT_CACHE_BYTES = 16 * 1024 * 1024
_SOURCE_DOCUMENT_CACHE_FIXED_BYTES = 512
_SOURCE_DOCUMENT_LINE_SPAN_BYTES = 128
_SOURCE_DOCUMENT_CACHE_CHARACTER_BYTES = 4
_T = TypeVar("_T")


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("navigation deadline expired")


def _index_revision_entries(
    revision: WorkspaceRevision,
    *,
    deadline: float | None,
) -> dict[str, RevisionEntry]:
    entries: dict[str, RevisionEntry] = {}
    for entry in revision.entries:
        _check_deadline(deadline)
        if isinstance(entry, RevisionEntry):
            entries[entry.path] = entry
        _check_deadline(deadline)
    _check_deadline(deadline)
    return entries


def _bounded_callback_values(
    values: Iterable[_T],
    *,
    deadline: float | None,
) -> tuple[tuple[_T, ...], bool]:
    iterator = iter(values)
    result: list[_T] = []
    for _ in range(_MAX_NAVIGATION_INPUT_VALUES):
        _check_deadline(deadline)
        try:
            value = next(iterator)
        except StopIteration:
            _check_deadline(deadline)
            return tuple(result), False
        _check_deadline(deadline)
        result.append(value)
    _check_deadline(deadline)
    try:
        next(iterator)
    except StopIteration:
        _check_deadline(deadline)
        return tuple(result), False
    _check_deadline(deadline)
    return tuple(result), True


def _require_text(
    value: object,
    label: str,
    *,
    optional: bool = False,
    nonempty: bool = False,
) -> None:
    if optional and value is None:
        return
    _require_string(value, label, nonempty=nonempty)


def _require_string(value: object, label: str, *, nonempty: bool) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    if nonempty and not value:
        raise ValueError(f"{label} must not be empty")


def _require_instance(value: object, kind: type | tuple[type, ...], message: str) -> None:
    if not isinstance(value, kind):
        raise TypeError(message)


def _optional_instance(value: object, kind: type, message: str) -> None:
    if value is not None and not isinstance(value, kind):
        raise TypeError(message)


def _records(values: Iterable[object], kind: type, message: str) -> tuple:
    records = tuple(values)
    if any(not isinstance(item, kind) for item in records):
        raise TypeError(message)
    return records


def _optional_integer(value: object, label: str, *, minimum: int) -> None:
    if value is not None:
        _require_integer(value, label, minimum=minimum)


def _require_page_limit(limit: object) -> None:
    _require_integer(limit, "limit", minimum=1)
    if limit > 100:  # type: ignore[operator]
        raise ValueError("limit must be between 1 and 100")


def _require_direction(capability: Capability, direction: object) -> None:
    _require_text(direction, "direction", optional=True)
    if direction is not None and direction not in {"incoming", "outgoing"}:
        raise ValueError("direction must be None, 'incoming', or 'outgoing'")
    _require_calls_direction(capability, direction)


def _require_calls_direction(capability: Capability, direction: object) -> None:
    if capability is Capability.CALLS and direction is None:
        raise ValueError("Capability.CALLS requires a direction")
    if capability is not Capability.CALLS and direction is not None:
        raise ValueError("direction must be None unless capability is CALLS")


def _require_deadline(deadline: object, type_message: str) -> None:
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        raise TypeError(type_message)
    if not math.isfinite(deadline):
        raise ValueError("deadline must be finite")


def _optional_deadline_expired(deadline: object) -> bool:
    if deadline is None:
        return False
    _require_deadline(deadline, "deadline must be a monotonic timestamp or None")
    return time.monotonic() >= deadline  # type: ignore[operator]


def _require_optional_callable(value: object, message: str) -> None:
    if value is not None and not callable(value):
        raise TypeError(message)


def _require_integer(value: object, label: str, *, minimum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    if value < minimum:
        raise ValueError(f"{label} must be at least {minimum}")


def _require_relative_path(value: object, label: str) -> None:
    try:
        validate_repository_relative_path(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise TypeError(f"{label} must be a string") from exc


@dataclass(frozen=True, slots=True)
class Provenance:
    source: str
    provider: str
    version: str
    observation: str

    def __post_init__(self) -> None:
        _require_text(self.source, "source", nonempty=True)
        _require_text(self.provider, "provider", nonempty=True)
        _require_text(self.version, "version")
        _require_text(self.observation, "observation", nonempty=True)


@dataclass(frozen=True, slots=True)
class NavigationLocation:
    path: str
    range: PositionRange
    line: int
    character: int
    containing_symbol: str | None
    signature: str | None
    resolution: ResolutionLabel
    provenance: tuple[Provenance, ...]

    def __post_init__(self) -> None:
        _require_relative_path(self.path, "path")
        _require_instance(self.range, PositionRange, "range must be a PositionRange")
        _require_integer(self.line, "line", minimum=1)
        _require_integer(self.character, "character", minimum=0)
        _require_text(self.containing_symbol, "containing_symbol", optional=True)
        _require_text(self.signature, "signature", optional=True)
        _require_instance(
            self.resolution, ResolutionLabel, "resolution must be a ResolutionLabel"
        )
        provenance = _records(
            self.provenance, Provenance, "provenance must contain Provenance records"
        )
        object.__setattr__(self, "provenance", provenance)


@dataclass(frozen=True, slots=True)
class NavigationDiagnostic:
    path: str
    range: PositionRange
    severity: DiagnosticSeverity
    code: str | None
    message: str
    related: tuple[NavigationLocation, ...]
    provenance: tuple[Provenance, ...]

    def __post_init__(self) -> None:
        _require_relative_path(self.path, "path")
        _require_instance(self.range, PositionRange, "range must be a PositionRange")
        _require_instance(
            self.severity, DiagnosticSeverity, "severity must be a DiagnosticSeverity"
        )
        _require_text(self.code, "code", optional=True)
        _require_text(self.message, "message")
        related = _records(
            self.related, NavigationLocation, "related must contain NavigationLocation records"
        )
        provenance = _records(
            self.provenance, Provenance, "provenance must contain Provenance records"
        )
        object.__setattr__(self, "related", related)
        object.__setattr__(self, "provenance", provenance)


@dataclass(frozen=True, slots=True)
class NavigationRequest:
    repository: RepositoryScope
    capability: Capability
    path: str
    line: int
    character: int
    offset: int = 0
    limit: int = 10
    direction: str | None = None

    def __post_init__(self) -> None:
        _require_instance(
            self.repository, RepositoryScope, "repository must be a RepositoryScope"
        )
        _require_instance(self.capability, Capability, "capability must be a Capability")
        _require_relative_path(self.path, "path")
        _require_integer(self.line, "line", minimum=1)
        _require_integer(self.character, "character", minimum=0)
        _require_integer(self.offset, "offset", minimum=0)
        _require_page_limit(self.limit)
        _require_direction(self.capability, self.direction)


@dataclass(frozen=True, slots=True)
class NavigationResult:
    status: NavigationStatus
    requested_capability: Capability
    effective_capability: Capability | None
    provider: str | None
    provider_version: str | None
    repository_id: str
    checkout_id: str
    workspace_revision_before: str
    workspace_revision_after: str
    document_version: int | None
    position_encoding: PositionEncoding | None
    readiness: str
    symbol: str | None
    total: int
    offset: int
    limit: int
    locations: tuple[NavigationLocation, ...]
    diagnostics: tuple[NavigationDiagnostic, ...]
    hover: str | None
    resolution: ResolutionLabel
    provenance: tuple[Provenance, ...]
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_instance(self.status, NavigationStatus, "status must be a NavigationStatus")
        _require_instance(
            self.requested_capability, Capability, "requested_capability must be a Capability"
        )
        _optional_instance(
            self.effective_capability,
            Capability,
            "effective_capability must be a Capability or None",
        )
        _require_text(self.provider, "provider", optional=True)
        _require_text(self.provider_version, "provider_version", optional=True)
        _require_text(self.repository_id, "repository_id", nonempty=True)
        _require_text(self.checkout_id, "checkout_id", nonempty=True)
        _require_text(self.workspace_revision_before, "workspace_revision_before")
        _require_text(self.workspace_revision_after, "workspace_revision_after")
        _optional_integer(self.document_version, "document_version", minimum=0)
        _optional_instance(
            self.position_encoding,
            PositionEncoding,
            "position_encoding must be a PositionEncoding or None",
        )
        _require_text(self.readiness, "readiness", nonempty=True)
        _require_text(self.symbol, "symbol", optional=True)
        _require_integer(self.total, "total", minimum=0)
        _require_integer(self.offset, "offset", minimum=0)
        _require_page_limit(self.limit)
        locations = _records(
            self.locations,
            NavigationLocation,
            "locations must contain NavigationLocation records",
        )
        diagnostics = _records(
            self.diagnostics,
            NavigationDiagnostic,
            "diagnostics must contain NavigationDiagnostic records",
        )
        _require_text(self.hover, "hover", optional=True)
        _require_instance(
            self.resolution, ResolutionLabel, "resolution must be a ResolutionLabel"
        )
        provenance = _records(
            self.provenance, Provenance, "provenance must contain Provenance records"
        )
        warnings = _records(self.warnings, str, "warnings must contain strings")
        object.__setattr__(self, "locations", locations)
        object.__setattr__(self, "diagnostics", diagnostics)
        object.__setattr__(self, "provenance", provenance)
        object.__setattr__(self, "warnings", warnings)


def _empty_result(
    request: NavigationRequest,
    status: NavigationStatus,
    *,
    revision_before: str,
    revision_after: str,
    provider: str | None = None,
    provider_version: str | None = None,
    readiness: str = "not_ready",
    resolution: ResolutionLabel = ResolutionLabel.UNRESOLVED,
    warnings: tuple[str, ...] = (),
) -> NavigationResult:
    return NavigationResult(
        status=status,
        requested_capability=request.capability,
        effective_capability=None,
        provider=provider,
        provider_version=provider_version,
        repository_id=request.repository.repository_id,
        checkout_id=request.repository.checkout_id,
        workspace_revision_before=revision_before,
        workspace_revision_after=revision_after,
        document_version=None,
        position_encoding=None,
        readiness=readiness,
        symbol=None,
        total=0,
        offset=request.offset,
        limit=request.limit,
        locations=(),
        diagnostics=(),
        hover=None,
        resolution=resolution,
        provenance=(),
        warnings=tuple(warnings),
    )


def _structural_result(
    repository: RepositoryScope,
    *,
    status: NavigationStatus,
    capability: Capability,
    revision_before: str,
    revision_after: str,
    readiness: str,
    symbol: str | None,
    locations: tuple[NavigationLocation, ...] = (),
    resolution: ResolutionLabel = ResolutionLabel.UNRESOLVED,
    provenance: tuple[Provenance, ...] = (),
    warnings: tuple[str, ...] = (),
) -> NavigationResult:
    return NavigationResult(
        status=status,
        requested_capability=capability,
        effective_capability=(
            capability
            if status not in {NavigationStatus.ERROR, NavigationStatus.TIMEOUT}
            else None
        ),
        provider=None,
        provider_version=None,
        repository_id=repository.repository_id,
        checkout_id=repository.checkout_id,
        workspace_revision_before=revision_before,
        workspace_revision_after=revision_after,
        document_version=None,
        position_encoding=None,
        readiness=readiness,
        symbol=symbol,
        total=len(locations),
        offset=0,
        limit=100,
        locations=tuple(locations),
        diagnostics=(),
        hover=None,
        resolution=resolution,
        provenance=tuple(provenance),
        warnings=tuple(warnings),
    )


class NavigationInterruption(Exception):
    """Raised to stop a navigation attempt cleanly without partial publication."""


class _RevisionMismatch(Exception):
    """Raised when bytes read inside an attempt do not match its revision."""


class _AttemptDocuments(dict[str, SourceDocument | None]):
    def __init__(
        self,
        touch: Callable[[tuple[str, str, int], SourceDocument], bool] | None = None,
    ) -> None:
        super().__init__()
        self.consumed: OrderedDict[str, SourceDocument] = OrderedDict()
        self._cached: dict[
            str, tuple[tuple[str, str, int], SourceDocument]
        ] = {}
        self._document_uris: dict[str, str] = {}
        self._normalized_sources: dict[str, RepositorySource | None] = {}
        self._touch = touch

    def normalize_provider_source(
        self,
        repository: RepositoryScope,
        uri: str,
    ) -> RepositorySource | None:
        if uri not in self._normalized_sources:
            self._normalized_sources[uri] = normalize_provider_uri(repository, uri)
        return self._normalized_sources[uri]

    def seed(
        self,
        uri: str,
        key: tuple[str, str, int],
        document: SourceDocument,
    ) -> None:
        self[uri] = document
        self._cached[uri] = (key, document)
        self._document_uris[document.path] = uri

    def consume(self, uri: str, document: SourceDocument) -> None:
        self._document_uris[document.path] = uri
        cached = self._cached.get(uri)
        if cached is not None and cached[1] is document:
            if self._touch is not None and self._touch(cached[0], document):
                return
        self.consumed[uri] = document
        self.consumed.move_to_end(uri)

    def consume_document(self, document: SourceDocument) -> None:
        uri = self._document_uris.get(document.path)
        if uri is not None:
            self.consume(uri, document)


@dataclass(frozen=True, slots=True)
class _ProviderOutcome:
    locations: tuple[LspLocation, ...]
    diagnostics: tuple[LspDiagnostic, ...]
    hover: ProviderHover | None
    coverage: str
    partial: bool
    effective_capability: Capability | None
    document_version: int | None
    failure: NavigationStatus | None = None
    references_require_graph: bool = False

    def __post_init__(self) -> None:
        if self.coverage not in {"provider_reported", "unsupported", "not_ready"}:
            raise ValueError("provider coverage is invalid")
        object.__setattr__(self, "locations", tuple(self.locations))
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))


def _range_from_lsp(
    location: LspLocation,
    document: SourceDocument,
    encoding: PositionEncoding,
) -> PositionRange | None:
    try:
        return document.to_byte_range(location.range, encoding)
    except (ValueError, TypeError):
        return None


@dataclass(frozen=True, slots=True)
class _LocationContext:
    """Everything one provider location needs to become a revision-proven fact."""

    repository: RepositoryScope
    resolution: ResolutionLabel
    provenance: tuple[Provenance, ...]
    documents: dict[str, SourceDocument | None]
    revision_entries: Mapping[str, RevisionEntry]
    encoding: PositionEncoding
    deadline: float


def _normalize_locations(
    repository: RepositoryScope,
    locations: tuple[LspLocation, ...],
    *,
    resolution: ResolutionLabel,
    provenance: tuple[Provenance, ...],
    documents: dict[str, SourceDocument | None],
    revision_entries: Mapping[str, RevisionEntry],
    encoding: PositionEncoding,
    deadline: float,
) -> tuple[tuple[NavigationLocation, ...], bool]:
    ctx = _LocationContext(
        repository, resolution, provenance, documents, revision_entries, encoding, deadline
    )
    normalized: list[NavigationLocation] = []
    partial = False
    _check_deadline(deadline)
    for location in locations:
        _check_deadline(deadline)
        value = _normalize_location(ctx, location)
        if value is None:
            partial = True
            continue
        normalized.append(value)
        _check_deadline(deadline)
    _check_deadline(deadline)
    return tuple(normalized), partial


def _normalize_location(ctx: _LocationContext, location: object) -> NavigationLocation | None:
    if not isinstance(location, LspLocation):
        return None
    _check_deadline(ctx.deadline)
    source = _provider_source(ctx.documents, ctx.repository, location.uri)
    _check_deadline(ctx.deadline)
    if source is None:
        return None
    return _normalize_sourced_location(ctx, location, source)


def _provider_source(
    documents: dict[str, SourceDocument | None],
    repository: RepositoryScope,
    uri: str,
) -> RepositorySource | None:
    if isinstance(documents, _AttemptDocuments):
        return documents.normalize_provider_source(repository, uri)
    return normalize_provider_uri(repository, uri)


def _normalize_sourced_location(
    ctx: _LocationContext,
    location: LspLocation,
    source: RepositorySource,
) -> NavigationLocation | None:
    document = _load_revision_source(
        source,
        ctx.revision_entries,
        ctx.documents,
        deadline=ctx.deadline,
    )
    _check_deadline(ctx.deadline)
    if document is None:
        return None
    _check_deadline(ctx.deadline)
    range_ = _range_from_lsp(location, document, ctx.encoding)
    _check_deadline(ctx.deadline)
    if range_ is None:
        return None
    line_start, _line_end = document.line_spans[location.range.start.line]
    return NavigationLocation(
        path=source.relative_path,
        range=range_,
        line=location.range.start.line + 1,
        character=range_.byte_start - line_start,
        containing_symbol=None,
        signature=None,
        resolution=ctx.resolution,
        provenance=ctx.provenance,
    )


def _load_revision_source(
    source: RepositorySource,
    revision_entries: Mapping[str, RevisionEntry],
    documents: dict[str, SourceDocument | None],
    *,
    deadline: float | None,
) -> SourceDocument | None:
    _check_deadline(deadline)
    if source.uri in documents:
        return _cached_revision_source(documents, source.uri)
    entry = revision_entries.get(source.relative_path)
    if not _loadable_entry(entry):
        documents[source.uri] = None
        return None
    content = _read_revision_bytes(source, entry, deadline)
    document = _revision_document(source, content, deadline)
    documents[source.uri] = document
    _consume_attempt_document(documents, source.uri, document)
    return document


def _cached_revision_source(
    documents: dict[str, SourceDocument | None], uri: str
) -> SourceDocument | None:
    document = documents[uri]
    _consume_attempt_document(documents, uri, document)
    return document


def _consume_attempt_document(
    documents: dict[str, SourceDocument | None],
    uri: str,
    document: SourceDocument | None,
) -> None:
    if isinstance(documents, _AttemptDocuments) and document is not None:
        documents.consume(uri, document)


def _loadable_entry(entry: object) -> bool:
    if not isinstance(entry, RevisionEntry) or entry.sha256 is None:
        return False
    return _bounded_source_size(entry.size)


def _bounded_source_size(size: object) -> bool:
    return (
        isinstance(size, int)
        and not isinstance(size, bool)
        and 0 <= size <= _MAX_NAVIGATION_SOURCE_BYTES
    )


def _read_revision_bytes(
    source: RepositorySource, entry: RevisionEntry, deadline: float | None
) -> bytes:
    """The recorded bytes of `source`, or `_RevisionMismatch` if they moved."""
    try:
        _check_deadline(deadline)
        content = read_stable_bytes(
            source.absolute_path,
            entry.size,
            label="navigation target",
            deadline=deadline,
        )
        _check_deadline(deadline)
    except TimeoutError:
        raise
    except (OSError, ValueError) as exc:
        raise _RevisionMismatch from exc
    _check_deadline(deadline)
    content_sha256 = hashlib_sha256(content)
    _check_deadline(deadline)
    if len(content) != entry.size or content_sha256 != entry.sha256:
        raise _RevisionMismatch
    return content


def _revision_document(
    source: RepositorySource, content: bytes, deadline: float | None
) -> SourceDocument | None:
    try:
        _check_deadline(deadline)
        document = SourceDocument.from_bytes(source.relative_path, content)
        _check_deadline(deadline)
    except (UnicodeError, ValueError, RuntimeError, TypeError):
        return None
    return document


def _byte_position(
    document: SourceDocument,
    byte_offset: int,
) -> tuple[int, int] | None:
    if not _decodable_prefix(document, byte_offset):
        return None
    for index, (start, end) in enumerate(document.line_spans):
        if start <= byte_offset <= end:
            return index + 1, byte_offset - start
    return None


def _decodable_prefix(document: SourceDocument, byte_offset: object) -> bool:
    if not _offset_within(byte_offset, len(document.content)):
        return False
    try:
        document.content[:byte_offset].decode("utf-8", errors="strict")
    except UnicodeError:
        return False
    return True


def _offset_within(offset: object, length: int) -> bool:
    return isinstance(offset, int) and not isinstance(offset, bool) and 0 <= offset <= length


def _provenance_key(value: Provenance) -> tuple[str, str, str, str]:
    return (value.source, value.provider, value.version, value.observation)


def _union_provenance(
    *groups: tuple[Provenance, ...],
) -> tuple[Provenance, ...]:
    values = {value for group in groups for value in group if isinstance(value, Provenance)}
    return tuple(sorted(values, key=_provenance_key))


def _normalize_structural_locations(
    repository: RepositoryScope,
    locations: tuple[NavigationLocation, ...],
    *,
    revision_entries: Mapping[str, RevisionEntry],
    documents: dict[str, SourceDocument | None],
    provenance: tuple[Provenance, ...],
    deadline: float | None,
) -> tuple[tuple[NavigationLocation, ...], bool]:
    normalized: list[NavigationLocation] = []
    partial = False
    _check_deadline(deadline)
    for location in locations:
        _check_deadline(deadline)
        value = _normalize_structural_location(
            repository, location, revision_entries, documents, provenance, deadline
        )
        if value is None:
            partial = True
            continue
        normalized.append(value)
        _check_deadline(deadline)
    _check_deadline(deadline)
    return tuple(normalized), partial


def _normalize_structural_location(
    repository: RepositoryScope,
    location: object,
    revision_entries: Mapping[str, RevisionEntry],
    documents: dict[str, SourceDocument | None],
    provenance: tuple[Provenance, ...],
    deadline: float | None,
) -> NavigationLocation | None:
    if not isinstance(location, NavigationLocation):
        return None
    document = _structural_document(
        repository, location.path, revision_entries, documents, deadline
    )
    if document is None or location.range.byte_end > len(document.content):
        return None
    return _graph_candidate_location(location, document, provenance, deadline)


def _structural_document(
    repository: RepositoryScope,
    path: str,
    revision_entries: Mapping[str, RevisionEntry],
    documents: dict[str, SourceDocument | None],
    deadline: float | None,
) -> SourceDocument | None:
    path_key = f"path:{path}"
    if path_key in documents:
        return _cached_path_document(documents, path_key)
    try:
        _check_deadline(deadline)
        source = resolve_repository_source(repository, path)
        _check_deadline(deadline)
    except TimeoutError:
        raise
    except (OSError, UnicodeError, ValueError, RuntimeError, TypeError):
        documents[path_key] = None
        return None
    document = _load_revision_source(
        source,
        revision_entries,
        documents,
        deadline=deadline,
    )
    documents[path_key] = document
    return document


def _cached_path_document(
    documents: dict[str, SourceDocument | None], path_key: str
) -> SourceDocument | None:
    document = documents[path_key]
    if isinstance(documents, _AttemptDocuments) and document is not None:
        documents.consume_document(document)
    return document


def _graph_candidate_location(
    location: NavigationLocation,
    document: SourceDocument,
    provenance: tuple[Provenance, ...],
    deadline: float | None,
) -> NavigationLocation | None:
    _check_deadline(deadline)
    start = _byte_position(document, location.range.byte_start)
    _check_deadline(deadline)
    end = _byte_position(document, location.range.byte_end)
    _check_deadline(deadline)
    if start is None or end is None:
        return None
    return NavigationLocation(
        document.path,
        location.range,
        start[0],
        start[1],
        location.containing_symbol,
        location.signature,
        ResolutionLabel.GRAPH_CANDIDATE,
        _union_provenance(location.provenance, provenance),
    )


def _load_revision_document(
    repository: RepositoryScope,
    path: str,
    revision_entries: Mapping[str, RevisionEntry],
    documents: dict[str, SourceDocument | None],
    *,
    deadline: float | None,
) -> tuple[RepositorySource | None, SourceDocument | None]:
    recorded_source = _loadable_entry(revision_entries.get(path))
    try:
        _check_deadline(deadline)
        source = resolve_repository_source(repository, path)
        _check_deadline(deadline)
    except TimeoutError:
        raise
    except (OSError, UnicodeError, ValueError, RuntimeError, TypeError):
        if recorded_source:
            raise _RevisionMismatch from None
        return None, None
    return source, _load_revision_source(
        source,
        revision_entries,
        documents,
        deadline=deadline,
    )


def _validate_revision_anchor(
    repository: RepositoryScope,
    anchor: SourceAnchor,
    revision_entries: Mapping[str, RevisionEntry],
    documents: dict[str, SourceDocument | None],
    *,
    deadline: float | None,
) -> bool:
    _source, document = _load_revision_document(
        repository,
        anchor.path,
        revision_entries,
        documents,
        deadline=deadline,
    )
    if document is None:
        return False
    try:
        _check_deadline(deadline)
        valid = (
            document.validate_anchor(
                line=anchor.line,
                character=anchor.utf8_character,
            )
            == anchor
        )
        _check_deadline(deadline)
        return valid
    except (TypeError, ValueError):
        return False


_DIAGNOSTIC_SEVERITIES: Mapping[int, DiagnosticSeverity] = MappingProxyType(
    {
        1: DiagnosticSeverity.ERROR,
        2: DiagnosticSeverity.WARNING,
        3: DiagnosticSeverity.INFORMATION,
        4: DiagnosticSeverity.HINT,
    }
)


def _diagnostic_key(diagnostic: NavigationDiagnostic) -> tuple[object, ...]:
    return (
        diagnostic.path,
        diagnostic.range.byte_start,
        diagnostic.range.byte_end,
        diagnostic.severity.value,
        diagnostic.code is not None,
        diagnostic.code or "",
        diagnostic.message,
        tuple(_location_tie_break(location) for location in diagnostic.related),
        tuple(_provenance_key(item) for item in diagnostic.provenance),
    )


def _normalize_diagnostics(
    repository: RepositoryScope,
    diagnostics: tuple[LspDiagnostic, ...],
    *,
    revision_entries: Mapping[str, RevisionEntry],
    documents: dict[str, SourceDocument | None],
    encoding: PositionEncoding,
    provenance: tuple[Provenance, ...],
    deadline: float,
) -> tuple[tuple[NavigationDiagnostic, ...], bool]:
    ctx = _LocationContext(
        repository,
        ResolutionLabel.LSP_CONFIRMED,
        provenance,
        documents,
        revision_entries,
        encoding,
        deadline,
    )
    return _DiagnosticNormalizer(ctx).run(diagnostics)


def _diagnostic_severity(diagnostic: LspDiagnostic) -> DiagnosticSeverity | None:
    if isinstance(diagnostic.severity, bool):
        return None
    return _DIAGNOSTIC_SEVERITIES.get(diagnostic.severity)


def _diagnostic_text_valid(diagnostic: LspDiagnostic) -> bool:
    code_valid = diagnostic.code is None or isinstance(diagnostic.code, str)
    return code_valid and isinstance(diagnostic.message, str)


def _related_pair(value: object) -> bool:
    return isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], LspLocation)


class _DiagnosticNormalizer:
    """Turns provider diagnostics into revision-proven facts under one fact budget."""

    def __init__(self, ctx: _LocationContext) -> None:
        self.ctx = ctx
        self.normalized: list[NavigationDiagnostic] = []
        self.partial = False
        self.fact_count = 0

    def run(
        self, diagnostics: tuple[LspDiagnostic, ...]
    ) -> tuple[tuple[NavigationDiagnostic, ...], bool]:
        deadline = self.ctx.deadline
        self.partial = len(diagnostics) > _MAX_NAVIGATION_FACTS
        for diagnostic in diagnostics[:_MAX_NAVIGATION_FACTS]:
            _check_deadline(deadline)
            if self.fact_count >= _MAX_NAVIGATION_FACTS:
                self.partial = True
                break
            self._add(diagnostic)
        _check_deadline(deadline)
        unique = {_diagnostic_key(value): value for value in self.normalized}
        _check_deadline(deadline)
        ordered = tuple(unique[key] for key in sorted(unique))
        _check_deadline(deadline)
        return ordered, self.partial

    def _add(self, diagnostic: object) -> None:
        located = self._diagnostic(diagnostic)
        if located is None:
            self.partial = True
            return
        value, related_count = located
        self.normalized.append(value)
        self.fact_count += 1 + related_count
        _check_deadline(self.ctx.deadline)

    def _diagnostic(self, diagnostic: object) -> tuple[NavigationDiagnostic, int] | None:
        if not isinstance(diagnostic, LspDiagnostic):
            return None
        severity = _diagnostic_severity(diagnostic)
        if severity is None or not _diagnostic_text_valid(diagnostic):
            return None
        return self._located_diagnostic(diagnostic, severity)

    def _located_diagnostic(
        self, diagnostic: LspDiagnostic, severity: DiagnosticSeverity
    ) -> tuple[NavigationDiagnostic, int] | None:
        location = self._single_location(LspLocation(diagnostic.uri, diagnostic.range))
        if location is None:
            return None
        related = self._related(diagnostic)
        value = NavigationDiagnostic(
            location.path,
            location.range,
            severity,
            diagnostic.code,
            diagnostic.message,
            _dedupe_locations(tuple(related), deadline=self.ctx.deadline),
            self.ctx.provenance,
        )
        return value, len(related)

    def _related(self, diagnostic: LspDiagnostic) -> list[NavigationLocation]:
        related: list[NavigationLocation] = []
        remaining_related = _MAX_NAVIGATION_FACTS - self.fact_count - 1
        if len(diagnostic.related) > remaining_related:
            self.partial = True
        for value in diagnostic.related[:remaining_related]:
            _check_deadline(self.ctx.deadline)
            location = self._related_location(value)
            if location is None:
                self.partial = True
                continue
            related.append(location)
            _check_deadline(self.ctx.deadline)
        return related

    def _related_location(self, value: object) -> NavigationLocation | None:
        if not _related_pair(value):
            return None
        return self._single_location(value[0])

    def _single_location(self, location: LspLocation) -> NavigationLocation | None:
        ctx = self.ctx
        locations, filtered = _normalize_locations(
            ctx.repository,
            (location,),
            resolution=ctx.resolution,
            provenance=ctx.provenance,
            documents=ctx.documents,
            revision_entries=ctx.revision_entries,
            encoding=ctx.encoding,
            deadline=ctx.deadline,
        )
        if filtered or len(locations) != 1:
            return None
        return locations[0]


def _location_provider(location: NavigationLocation) -> str:
    return "|".join(sorted({item.provider for item in location.provenance}))


def _location_key(location: NavigationLocation) -> tuple[object, ...]:
    return (
        location.path,
        location.range.byte_start,
        location.range.byte_end,
        location.resolution.value,
        _location_provider(location),
    )


def _location_tie_break(location: NavigationLocation) -> tuple[object, ...]:
    return (
        *_location_key(location),
        location.line,
        location.character,
        (
            location.containing_symbol is not None,
            location.containing_symbol or "",
        ),
        (location.signature is not None, location.signature or ""),
        tuple(_provenance_key(item) for item in location.provenance),
    )


def _dedupe_locations(
    locations: tuple[NavigationLocation, ...],
    *,
    deadline: float | None = None,
) -> tuple[NavigationLocation, ...]:
    result: dict[tuple[object, ...], NavigationLocation] = {}
    _check_deadline(deadline)
    ordered = sorted(locations, key=_location_tie_break)
    _check_deadline(deadline)
    for location in ordered:
        _check_deadline(deadline)
        key = _location_key(location)
        if key not in result:
            result[key] = location
        _check_deadline(deadline)
    return tuple(result.values())


def _graph_only_candidates(
    graph_locations: tuple[NavigationLocation, ...],
    lsp_locations: tuple[NavigationLocation, ...],
    graph_provenance: tuple[Provenance, ...],
) -> tuple[NavigationLocation, ...]:
    lsp_keys = {
        (location.path, location.range.byte_start, location.range.byte_end)
        for location in lsp_locations
    }
    appended: list[NavigationLocation] = []
    for location in graph_locations:
        if (location.path, location.range.byte_start, location.range.byte_end) in lsp_keys:
            continue
        appended.append(
            NavigationLocation(
                path=location.path,
                range=location.range,
                line=location.line,
                character=location.character,
                containing_symbol=location.containing_symbol,
                signature=location.signature,
                resolution=ResolutionLabel.GRAPH_CANDIDATE,
                provenance=_union_provenance(location.provenance, graph_provenance),
            )
        )
    return tuple(appended)


def _span_key(location: NavigationLocation) -> tuple[str, int, int]:
    return (location.path, location.range.byte_start, location.range.byte_end)


def _confirmed_location(
    location: NavigationLocation,
    graph_by_span: Mapping[tuple[str, int, int], list[NavigationLocation]],
    matched: set[tuple[str, int, int]],
) -> NavigationLocation:
    key = _span_key(location)
    matches = graph_by_span.get(key, ())
    if not matches:
        return location
    matched.add(key)
    return replace(
        location,
        resolution=ResolutionLabel.LSP_AND_GRAPH,
        provenance=_union_provenance(
            location.provenance,
            *(match.provenance for match in matches),
        ),
    )


def _merge_locations(
    lsp_locations: tuple[NavigationLocation, ...],
    graph_locations: tuple[NavigationLocation, ...],
    *,
    deadline: float | None = None,
) -> tuple[tuple[NavigationLocation, ...], bool]:
    lsp = _dedupe_locations(lsp_locations, deadline=deadline)
    graph = _dedupe_locations(graph_locations, deadline=deadline)
    graph_by_span: dict[tuple[str, int, int], list[NavigationLocation]] = {}
    for location in graph:
        _check_deadline(deadline)
        graph_by_span.setdefault(_span_key(location), []).append(location)
    matched: set[tuple[str, int, int]] = set()
    confirmed: list[NavigationLocation] = []
    for location in lsp:
        _check_deadline(deadline)
        confirmed.append(_confirmed_location(location, graph_by_span, matched))
        _check_deadline(deadline)
    _check_deadline(deadline)
    candidates = [
        replace(location, resolution=ResolutionLabel.GRAPH_CANDIDATE)
        for location in graph
        if _span_key(location) not in matched
    ]
    _check_deadline(deadline)
    merged = (*confirmed, *candidates)
    truncated = len(merged) > _MAX_NAVIGATION_FACTS
    return tuple(merged[:_MAX_NAVIGATION_FACTS]), truncated


def _compute_revision(
    repository: RepositoryScope,
    *,
    deadline: float | None,
) -> WorkspaceRevision:
    return compute_workspace_revision(repository, deadline=deadline)


def _compute_post_revision(
    repository: RepositoryScope,
    expected: WorkspaceRevision,
    *,
    deadline: float | None,
) -> WorkspaceRevision:
    try:
        if verify_workspace_revision_unchanged(
            repository,
            expected,
            deadline=deadline,
        ):
            return expected
    except (TypeError, ValueError):
        pass
    return _compute_revision(repository, deadline=deadline)


def _require_session_binding(
    repository: RepositoryScope, session: PyrightSession, identity: PyrightIdentity
) -> None:
    if identity != session.identity:
        raise ValueError("identity must match the Pyright session identity")
    session_repository = session._repository
    if (
        repository.repository_id != session_repository.repository_id
        or repository.checkout_id != session_repository.checkout_id
    ):
        raise ValueError("repository must match the Pyright session repository")


def _file_uri(uri: object) -> bool:
    return isinstance(uri, str) and uri.startswith("file:")


def _entry_matches_document(entry: object, document: SourceDocument) -> bool:
    if not isinstance(entry, RevisionEntry) or entry.path != document.path:
        return False
    if not isinstance(entry.sha256, str) or entry.sha256 != document.source_sha256:
        return False
    return _entry_size_matches(entry.size, document)


def _entry_size_matches(size: object, document: SourceDocument) -> bool:
    return _bounded_source_size(size) and size == len(document.content)


class CodeNavigation:
    """Freshness-proven navigation facade over one Pyright provider session."""

    def __init__(
        self,
        repository: RepositoryScope,
        session: PyrightSession,
        identity: PyrightIdentity,
        *,
        structural_candidates: Callable[
            [NavigationRequest, float], tuple[NavigationLocation, ...]
        ]
        | None = None,
        symbol_resolver: Callable[
            [str, RepositoryScope, float | None], tuple[NavigationLocation, ...]
        ]
        | None = None,
        edge_verifier: Callable[
            [SourceAnchor, SourceAnchor, RepositoryScope, float], bool
        ]
        | None = None,
    ) -> None:
        _require_instance(repository, RepositoryScope, "repository must be a RepositoryScope")
        _require_instance(session, PyrightSession, "session must be a PyrightSession")
        _require_instance(identity, PyrightIdentity, "identity must be a PyrightIdentity")
        _require_session_binding(repository, session, identity)
        _require_optional_callable(
            structural_candidates, "structural_candidates must be callable or None"
        )
        _require_optional_callable(symbol_resolver, "symbol_resolver must be callable or None")
        _require_optional_callable(edge_verifier, "edge_verifier must be callable or None")
        self._repository = repository
        self._session = session
        self._identity = identity
        self._structural_candidates = structural_candidates
        self._symbol_resolver = symbol_resolver
        self._edge_verifier = edge_verifier
        self._lock = threading.Lock()
        self._source_document_cache: OrderedDict[
            tuple[str, str, int], SourceDocument
        ] = OrderedDict()
        self._source_document_cache_bytes = 0
        self._closed = False

    @property
    def repository(self) -> RepositoryScope:
        return self._repository

    @property
    def provider(self) -> str:
        return "pyright"

    @property
    def provider_version(self) -> str | None:
        return self._identity.version

    @staticmethod
    def _source_document_cache_key(
        uri: str,
        document: SourceDocument,
        revision_entries: Mapping[str, RevisionEntry],
    ) -> tuple[str, str, int] | None:
        entry = revision_entries.get(document.path)
        if not _file_uri(uri) or not _entry_matches_document(entry, document):
            return None
        return uri, entry.sha256, entry.size

    @staticmethod
    def _source_document_retained_bytes(
        key: tuple[str, str, int],
        document: SourceDocument,
    ) -> int:
        retained_characters = (
            len(key[0])
            + len(key[1])
            + len(document.path)
            + len(document.source_sha256)
        )
        return (
            _SOURCE_DOCUMENT_CACHE_FIXED_BYTES
            + len(document.content)
            + len(document.line_spans) * _SOURCE_DOCUMENT_LINE_SPAN_BYTES
            + retained_characters * _SOURCE_DOCUMENT_CACHE_CHARACTER_BYTES
        )

    def _touch_source_document(
        self,
        key: tuple[str, str, int],
        document: SourceDocument,
    ) -> bool:
        with self._lock:
            if (
                not self._closed
                and self._source_document_cache.get(key) is document
            ):
                self._source_document_cache.move_to_end(key)
                return True
            return False

    def _seed_source_documents(
        self,
        revision_entries: Mapping[str, RevisionEntry],
    ) -> _AttemptDocuments:
        documents = _AttemptDocuments(self._touch_source_document)
        with self._lock:
            if self._closed:
                return documents
            for key, document in self._source_document_cache.items():
                if self._source_document_cache_key(
                    key[0], document, revision_entries
                ) == key:
                    documents.seed(key[0], key, document)
        return documents

    def _publish_source_documents(
        self,
        revision_entries: Mapping[str, RevisionEntry],
        documents: _AttemptDocuments,
    ) -> None:
        candidates = self._publishable_documents(revision_entries, documents)
        if not candidates:
            return
        with self._lock:
            if self._closed:
                return
            for key, document in candidates.items():
                self._cache_source_document(key, document)

    def _publishable_documents(
        self,
        revision_entries: Mapping[str, RevisionEntry],
        documents: _AttemptDocuments,
    ) -> dict[tuple[str, str, int], SourceDocument]:
        candidates: dict[tuple[str, str, int], SourceDocument] = {}
        for uri, document in documents.consumed.items():
            key = self._source_document_cache_key(uri, document, revision_entries)
            if key is not None and self._fits_source_document_cache(key, document):
                candidates[key] = document
        return candidates

    def _fits_source_document_cache(
        self, key: tuple[str, str, int], document: SourceDocument
    ) -> bool:
        retained = self._source_document_retained_bytes(key, document)
        return retained <= _MAX_SOURCE_DOCUMENT_CACHE_BYTES

    def _cache_source_document(self, key: tuple[str, str, int], document: SourceDocument) -> None:
        """Caller holds the lock."""
        previous = self._source_document_cache.get(key)
        if previous is document:
            self._source_document_cache.move_to_end(key)
            return
        if previous is not None:
            self._source_document_cache.pop(key)
            self._source_document_cache_bytes -= (
                self._source_document_retained_bytes(key, previous)
            )
        self._source_document_cache[key] = document
        self._source_document_cache_bytes += (
            self._source_document_retained_bytes(key, document)
        )
        self._evict_source_documents()

    def _evict_source_documents(self) -> None:
        """Caller holds the lock."""
        while (
            len(self._source_document_cache) > _MAX_SOURCE_DOCUMENT_CACHE_ENTRIES
            or self._source_document_cache_bytes > _MAX_SOURCE_DOCUMENT_CACHE_BYTES
        ):
            old_key, old_document = self._source_document_cache.popitem(last=False)
            self._source_document_cache_bytes -= (
                self._source_document_retained_bytes(old_key, old_document)
            )

    def query(
        self,
        request: NavigationRequest,
        *,
        deadline: float,
    ) -> NavigationResult:
        _require_instance(request, NavigationRequest, "request must be a NavigationRequest")
        _require_deadline(deadline, "deadline must be a monotonic timestamp")
        if request.repository.checkout_id != self._repository.checkout_id:
            raise ValueError("request must target this navigation repository")
        refusal = self._query_refusal(request, deadline)
        if refusal is not None:
            return refusal
        return _QueryAttempts(self, request, deadline).run()

    def _query_refusal(
        self, request: NavigationRequest, deadline: float
    ) -> NavigationResult | None:
        if request.capability not in _CAPABILITY_DIRECTION:
            return _empty_result(
                request,
                NavigationStatus.UNSUPPORTED,
                revision_before="",
                revision_after="",
                readiness=self._session.readiness,
                resolution=ResolutionLabel.UNSUPPORTED,
                warnings=("capability is unsupported",),
            )
        if time.monotonic() >= deadline:
            return _empty_result(
                request,
                NavigationStatus.TIMEOUT,
                revision_before="",
                revision_after="",
                readiness=self._session.readiness,
                warnings=("deadline expired before query",),
            )
        return None

    def _require_own_checkout(self, repository: RepositoryScope) -> None:
        if repository.checkout_id != self._repository.checkout_id:
            raise ValueError("repository must match this navigation repository")

    def _attempt_query(
        self,
        request: NavigationRequest,
        before_revision: WorkspaceRevision,
        *,
        deadline: float,
    ) -> NavigationResult:
        try:
            return _QueryAttempt(self, request, before_revision, deadline).run()
        except _Finished as finished:
            return finished.result

    def _provider_request(
        self,
        request: NavigationRequest,
        anchor: SourceAnchor,
        document: OpenDocument,
        *,
        deadline: float,
    ) -> _ProviderOutcome:
        method = _LOCATION_REQUESTS.get(request.capability)
        if method is not None:
            return self._location_request(method, request.capability, anchor, document, deadline)
        handler = {
            Capability.TYPES: self._types_request,
            Capability.DIAGNOSTICS: self._diagnostics_request,
            Capability.CALLS: self._calls_request,
        }.get(request.capability)
        if handler is None:
            return _ProviderOutcome((), (), None, "unsupported", True, None, document.version)
        return handler(request, anchor, document, deadline)

    def _location_request(
        self,
        method: str,
        capability: Capability,
        anchor: SourceAnchor,
        document: OpenDocument,
        deadline: float,
    ) -> _ProviderOutcome:
        _check_deadline(deadline)
        result = getattr(self._session, method)(anchor, deadline=deadline)
        _check_deadline(deadline)
        return _location_outcome(result, capability, document, deadline)

    def _types_request(
        self,
        request: NavigationRequest,
        anchor: SourceAnchor,
        document: OpenDocument,
        deadline: float,
    ) -> _ProviderOutcome:
        type_result, type_failure = _guarded_provider_call(
            lambda: self._session.type_definition(anchor, deadline=deadline),
            lambda: ProviderLocations((), "not_ready", True),
            deadline,
        )
        hover_result, hover_failure = _guarded_provider_call(
            lambda: self._session.hover(anchor, deadline=deadline),
            lambda: ProviderHover(None, None, True),
            deadline,
        )
        return _types_outcome(
            request.capability, document, type_result, type_failure, hover_result, hover_failure
        )

    def _diagnostics_request(
        self,
        request: NavigationRequest,
        anchor: SourceAnchor,
        document: OpenDocument,
        deadline: float,
    ) -> _ProviderOutcome:
        _check_deadline(deadline)
        result: ProviderDiagnostics = self._session.diagnostics(
            request.path, deadline=deadline
        )
        _check_deadline(deadline)
        return _ProviderOutcome(
            (),
            result.diagnostics,
            None,
            _diagnostics_coverage(result),
            result.partial,
            request.capability,
            result.document_version,
        )

    def _calls_request(
        self,
        request: NavigationRequest,
        anchor: SourceAnchor,
        document: OpenDocument,
        deadline: float,
    ) -> _ProviderOutcome:
        _check_deadline(deadline)
        calls = self._direction_calls(request.direction, anchor, deadline)
        _check_deadline(deadline)
        if calls.coverage == "unsupported" and self._structural_candidates is not None:
            return self._reference_fallback(anchor, document, deadline)
        return _location_outcome(calls, request.capability, document, deadline)

    def _direction_calls(
        self, direction: str | None, anchor: SourceAnchor, deadline: float
    ) -> ProviderLocations:
        if direction == "incoming":
            return self._session.incoming_calls(anchor, deadline=deadline)
        return self._session.outgoing_calls(anchor, deadline=deadline)

    def _reference_fallback(
        self, anchor: SourceAnchor, document: OpenDocument, deadline: float
    ) -> _ProviderOutcome:
        """No call hierarchy: references, kept only where the graph proves a call."""
        _check_deadline(deadline)
        references = self._session.references(anchor, deadline=deadline)
        _check_deadline(deadline)
        return _ProviderOutcome(
            references.locations,
            (),
            None,
            references.coverage,
            True,
            _reported_capability(references.coverage, Capability.REFERENCES),
            document.version,
            references_require_graph=True,
        )

    def resolve_symbol(
        self,
        symbol: str,
        *,
        repository: RepositoryScope,
        deadline: float | None = None,
    ) -> NavigationResult:
        _require_symbol(symbol)
        _require_instance(repository, RepositoryScope, "repository must be a RepositoryScope")
        self._require_own_checkout(repository)
        run = _StructuralRun(
            self, repository, Capability.DEFINITIONS, symbol, deadline, _SYMBOL_MESSAGES
        )
        if _optional_deadline_expired(deadline):
            return run.result(
                NavigationStatus.TIMEOUT,
                "",
                "",
                warnings=("deadline expired before symbol resolution",),
            )
        return run.run(self._symbol_attempt)

    def _symbol_attempt(self, run: _StructuralRun) -> NavigationResult:
        raw_candidates, input_truncated, resolver_failure = self._resolver_values(run)
        documents = self._seed_source_documents(run.revision_entries)
        candidates, filtered = _symbol_candidates(run, raw_candidates, documents)
        candidates = _dedupe_locations(candidates, deadline=run.deadline)
        _check_deadline(run.deadline)
        truncated = len(candidates) > _MAX_NAVIGATION_FACTS
        candidates, resolution = _labeled_candidates(
            candidates[:_MAX_NAVIGATION_FACTS], input_truncated
        )
        _check_deadline(run.deadline)
        run.prove_unchanged()
        warnings = _symbol_warnings(
            resolver_failure, filtered, input_truncated, truncated, candidates
        )
        provenance = _union_provenance(
            tuple(item for value in candidates for item in value.provenance)
        )
        _check_deadline(run.deadline)
        result = run.result(
            _symbol_status(resolver_failure, candidates, warnings),
            run.revision_before,
            run.revision_after,
            locations=candidates,
            resolution=resolution,
            provenance=provenance,
            warnings=warnings,
        )
        run.require_constructed()
        if result.status in {NavigationStatus.OK, NavigationStatus.PARTIAL}:
            self._publish_source_documents(run.revision_entries, documents)
        return result

    def _resolver_values(
        self, run: _StructuralRun
    ) -> tuple[tuple[NavigationLocation, ...], bool, NavigationStatus | None]:
        """Bounded resolver output; a failure keeps whatever was already bounded."""
        if self._symbol_resolver is None:
            return (), False, None
        raw_candidates: tuple[NavigationLocation, ...] = ()
        input_truncated = False
        try:
            _check_deadline(run.deadline)
            resolver_values = self._symbol_resolver(run.symbol, run.repository, run.deadline)
            _check_deadline(run.deadline)
            raw_candidates, input_truncated = _bounded_callback_values(
                resolver_values,
                deadline=run.deadline,
            )
            _check_deadline(run.deadline)
        except TimeoutError:
            return raw_candidates, input_truncated, NavigationStatus.TIMEOUT
        except NavigationInterruption:
            raise
        except Exception:
            return raw_candidates, input_truncated, NavigationStatus.ERROR
        return raw_candidates, input_truncated, None

    def verify_edge(
        self,
        source: SourceAnchor,
        target: SourceAnchor,
        *,
        repository: RepositoryScope,
        deadline: float,
    ) -> NavigationResult:
        _require_instance(source, SourceAnchor, "source must be a SourceAnchor")
        _require_instance(target, SourceAnchor, "target must be a SourceAnchor")
        _require_instance(repository, RepositoryScope, "repository must be a RepositoryScope")
        self._require_own_checkout(repository)
        _require_deadline(deadline, "deadline must be a monotonic timestamp")
        run = _StructuralRun(self, repository, Capability.CALLS, None, deadline, _EDGE_MESSAGES)
        if time.monotonic() >= deadline:
            return run.result(
                NavigationStatus.TIMEOUT,
                "",
                "",
                warnings=("deadline expired before edge verification",),
            )
        return run.run(lambda current: self._edge_attempt(current, source, target))

    def _edge_attempt(
        self, run: _StructuralRun, source: SourceAnchor, target: SourceAnchor
    ) -> NavigationResult:
        documents = self._seed_source_documents(run.revision_entries)
        anchors_valid = _edge_anchors_valid(run, source, target, documents)
        verified, verifier_failure = self._edge_verdict(run, source, target, anchors_valid)
        run.prove_unchanged()
        _require_edge_proof(run, anchors_valid, verifier_failure)
        if verified is not True:
            self._publish_source_documents(run.revision_entries, documents)
            return run.result(
                NavigationStatus.PARTIAL,
                run.revision_before,
                run.revision_after,
                warnings=("no structural edge proof",),
            )
        return self._confirmed_edge(run, target, documents)

    def _edge_verdict(
        self,
        run: _StructuralRun,
        source: SourceAnchor,
        target: SourceAnchor,
        anchors_valid: bool,
    ) -> tuple[bool | None, NavigationStatus | None]:
        if not anchors_valid or self._edge_verifier is None:
            return None, None
        return self._call_edge_verifier(run, source, target)

    def _call_edge_verifier(
        self, run: _StructuralRun, source: SourceAnchor, target: SourceAnchor
    ) -> tuple[bool | None, NavigationStatus | None]:
        try:
            _check_deadline(run.deadline)
            value = self._edge_verifier(source, target, run.repository, run.deadline)
            _check_deadline(run.deadline)
            _require_instance(value, bool, "edge verifier result must be boolean")
        except TimeoutError:
            return None, NavigationStatus.TIMEOUT
        except NavigationInterruption:
            raise
        except Exception:
            return None, NavigationStatus.ERROR
        return value, None

    def _confirmed_edge(
        self, run: _StructuralRun, target: SourceAnchor, documents: _AttemptDocuments
    ) -> NavigationResult:
        provenance = _graph_provenance("edge_verification")
        location = NavigationLocation(
            target.path,
            PositionRange(target.byte_offset, target.byte_offset),
            target.line,
            target.utf8_character,
            None,
            None,
            ResolutionLabel.GRAPH_CONFIRMED,
            provenance,
        )
        _check_deadline(run.deadline)
        result = run.result(
            NavigationStatus.OK,
            run.revision_before,
            run.revision_after,
            locations=(location,),
            resolution=ResolutionLabel.GRAPH_CONFIRMED,
            provenance=provenance,
        )
        run.require_constructed()
        self._publish_source_documents(run.revision_entries, documents)
        return result

    def close(self, *, deadline: float) -> None:
        try:
            self._session.close(deadline=deadline)
        finally:
            with self._lock:
                self._closed = True
                self._source_document_cache.clear()
                self._source_document_cache_bytes = 0


def hashlib_sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


class _Finished(Exception):
    """Ends one navigation attempt early with the result it has already decided."""

    def __init__(self, result: NavigationResult) -> None:
        super().__init__(result.status.value)
        self.result = result


class _Retry(Exception):
    """The workspace moved under the first attempt; the second one starts over."""


_LOCATION_REQUESTS: Mapping[Capability, str] = MappingProxyType(
    {
        Capability.DEFINITIONS: "definition",
        Capability.REFERENCES: "references",
        Capability.IMPLEMENTATIONS: "implementations",
        Capability.TYPE_DEFINITIONS: "type_definition",
    }
)
_FAILURE_WARNINGS: Mapping[NavigationStatus, str] = MappingProxyType(
    {
        NavigationStatus.TIMEOUT: "provider request timed out",
        NavigationStatus.ERROR: "provider request failed",
        NavigationStatus.NOT_READY: "provider is not ready",
    }
)
_COVERAGE_WARNINGS: Mapping[str, str] = MappingProxyType(
    {
        "unsupported": "provider capability is unsupported",
        "not_ready": "provider is not ready",
    }
)
_COVERAGE_STATUS: Mapping[str, NavigationStatus] = MappingProxyType(
    {
        "unsupported": NavigationStatus.UNSUPPORTED,
        "not_ready": NavigationStatus.NOT_READY,
    }
)
_VERIFIER_WARNINGS: Mapping[NavigationStatus, str] = MappingProxyType(
    {
        NavigationStatus.TIMEOUT: "edge verifier timed out",
        NavigationStatus.ERROR: "edge verifier failed",
    }
)
_SYMBOL_MESSAGES: Mapping[str, str] = MappingProxyType(
    {
        "revision_timeout": "symbol revision computation timed out",
        "revision_failed": "symbol revision computation failed",
        "revision_unavailable": "symbol revision is unavailable",
        "indexing_timeout": "symbol revision indexing timed out",
        "post_timeout": "post-resolution revision timed out",
        "post_failed": "post-resolution revision failed",
        "changed": "workspace changed across symbol resolution",
        "construction_timeout": "symbol result construction timed out",
        "loop": "symbol resolution attempt loop must return",
    }
)
_EDGE_MESSAGES: Mapping[str, str] = MappingProxyType(
    {
        "revision_timeout": "edge revision computation timed out",
        "revision_failed": "edge revision computation failed",
        "revision_unavailable": "edge revision is unavailable",
        "indexing_timeout": "edge revision indexing timed out",
        "post_timeout": "post-edge revision timed out",
        "post_failed": "post-edge revision failed",
        "changed": "workspace changed across edge verification",
        "construction_timeout": "edge result construction timed out",
        "loop": "edge verification attempt loop must return",
    }
)


def _graph_provenance(observation: str) -> tuple[Provenance, ...]:
    return (
        Provenance(
            source="graph",
            provider="evidence-graph",
            version="structural",
            observation=observation,
        ),
    )


def _flagged(flags: Iterable[tuple[object, str]]) -> tuple[str, ...]:
    return tuple(message for flag, message in flags if flag)


def _require_symbol(symbol: object) -> None:
    if not isinstance(symbol, str) or not symbol:
        raise ValueError("symbol must be a non-empty string")


def _reported_capability(coverage: str, capability: Capability) -> Capability | None:
    if coverage == "unsupported":
        return None
    return capability


def _location_outcome(
    result: ProviderLocations,
    capability: Capability,
    document: OpenDocument,
    deadline: float,
) -> _ProviderOutcome:
    _check_deadline(deadline)
    return _ProviderOutcome(
        result.locations,
        (),
        None,
        result.coverage,
        result.partial,
        _reported_capability(result.coverage, capability),
        document.version,
    )


def _guarded_provider_call(
    call: Callable[[], _T],
    fallback: Callable[[], _T],
    deadline: float,
) -> tuple[_T, NavigationStatus | None]:
    """One provider call whose failure becomes a fallback value and a status."""
    try:
        _check_deadline(deadline)
        result = call()
        _check_deadline(deadline)
    except TimeoutError:
        return fallback(), NavigationStatus.TIMEOUT
    except (OSError, ValueError, RuntimeError):
        return fallback(), NavigationStatus.ERROR
    return result, None


def _hover_reported(hover: ProviderHover) -> bool:
    return hover.contents is not None or hover.range is not None or not hover.partial


def _combined_failure(
    type_failure: NavigationStatus | None, hover_failure: NavigationStatus | None
) -> NavigationStatus | None:
    if NavigationStatus.TIMEOUT in {type_failure, hover_failure}:
        return NavigationStatus.TIMEOUT
    return type_failure or hover_failure


def _types_outcome(
    capability: Capability,
    document: OpenDocument,
    type_result: ProviderLocations,
    type_failure: NavigationStatus | None,
    hover_result: ProviderHover,
    hover_failure: NavigationStatus | None,
) -> _ProviderOutcome:
    """A type answer needs either the type definition or a real hover."""
    type_available = type_failure is None and type_result.coverage == "provider_reported"
    hover_available = hover_failure is None and _hover_reported(hover_result)
    partial = _types_partial(type_result, hover_result, type_available, hover_available)
    if type_available or hover_available:
        return _ProviderOutcome(
            type_result.locations, (), hover_result, "provider_reported", partial,
            capability, document.version, None,
        )
    return _ProviderOutcome(
        type_result.locations,
        (),
        hover_result,
        type_result.coverage,
        partial,
        _reported_capability(type_result.coverage, capability),
        document.version,
        _combined_failure(type_failure, hover_failure),
    )


def _types_partial(
    type_result: ProviderLocations,
    hover_result: ProviderHover,
    type_available: bool,
    hover_available: bool,
) -> bool:
    return (
        type_result.partial
        or hover_result.partial
        or not type_available
        or not hover_available
    )


def _diagnostics_coverage(result: ProviderDiagnostics) -> str:
    if result.document_version is not None or result.diagnostics:
        return "provider_reported"
    return "not_ready"


def _provider_warnings(
    provider_failure: NavigationStatus | None, outcome: _ProviderOutcome
) -> tuple[str, ...]:
    if provider_failure is not None:
        return ("provider setup is not ready",)
    warning = _outcome_warning(outcome)
    return () if warning is None else (warning,)


def _outcome_warning(outcome: _ProviderOutcome) -> str | None:
    warning = _FAILURE_WARNINGS.get(outcome.failure) or _COVERAGE_WARNINGS.get(outcome.coverage)
    if warning is None and outcome.partial:
        return "provider reported partial results"
    return warning


def _within_graph_spans(
    locations: tuple[NavigationLocation, ...],
    graph_locations: tuple[NavigationLocation, ...],
) -> tuple[NavigationLocation, ...]:
    graph_spans = {_span_key(location) for location in graph_locations}
    return tuple(location for location in locations if _span_key(location) in graph_spans)


def _fit_fact_budget(
    locations: tuple[NavigationLocation, ...],
    diagnostics: tuple[NavigationDiagnostic, ...],
) -> tuple[tuple[NavigationLocation, ...], bool]:
    """Locations that still fit after the diagnostics took their share of the fact limit."""
    diagnostic_fact_count = sum(1 + len(diagnostic.related) for diagnostic in diagnostics)
    remaining_locations = max(0, _MAX_NAVIGATION_FACTS - diagnostic_fact_count)
    if len(locations) > remaining_locations:
        return locations[:remaining_locations], True
    return locations, False


def _merged_resolution(locations: tuple[NavigationLocation, ...]) -> ResolutionLabel:
    for label in (ResolutionLabel.LSP_AND_GRAPH, ResolutionLabel.LSP_CONFIRMED):
        if any(location.resolution is label for location in locations):
            return label
    return ResolutionLabel.GRAPH_CANDIDATE if locations else ResolutionLabel.UNRESOLVED


def _structural_provenance(location: NavigationLocation) -> bool:
    return any(item.provider == "evidence-graph" for item in location.provenance)


def _query_status(
    outcome: _ProviderOutcome, useful_structural: bool, warnings: tuple[str, ...]
) -> NavigationStatus:
    unavailable = _unavailable_status(outcome)
    if unavailable is not None:
        return NavigationStatus.PARTIAL if useful_structural else unavailable
    return NavigationStatus.OK if not warnings else NavigationStatus.PARTIAL


def _unavailable_status(outcome: _ProviderOutcome) -> NavigationStatus | None:
    """The provider's own failure, else the status its coverage stands for."""
    if outcome.failure is not None:
        return outcome.failure
    return _COVERAGE_STATUS.get(outcome.coverage)


def _fact_provenance(
    facts: Iterable[NavigationLocation | NavigationDiagnostic],
) -> list[Provenance]:
    return [item for fact in facts for item in fact.provenance]


def _provider_reported(outcome: _ProviderOutcome) -> bool:
    return outcome.failure is None and outcome.coverage == "provider_reported"


def _require_unchanged_entry(
    source_entry: RevisionEntry | None, document: OpenDocument, source_sha256: str
) -> None:
    if source_entry is None or source_entry.sha256 is None:
        return
    if source_entry.size != len(document.content) or source_entry.sha256 != source_sha256:
        raise _RevisionMismatch


def _entry_binds(
    source_entry: RevisionEntry | None, document: OpenDocument, source_sha256: str
) -> bool:
    if source_entry is None or source_entry.sha256 is None:
        return False
    return (
        source_entry.size == len(document.content)
        and document.source_sha256 == source_sha256
    )


def _hover_range_valid(
    document: SourceDocument, hover_range: object, encoding: PositionEncoding, deadline: float
) -> bool:
    try:
        _check_deadline(deadline)
        document.to_byte_range(hover_range, encoding)
        _check_deadline(deadline)
    except (TypeError, ValueError):
        return False
    return True


def _not_ready_outcome(failure: NavigationStatus) -> _ProviderOutcome:
    return _ProviderOutcome((), (), None, "not_ready", True, None, None, failure)


class _QueryAttempts:
    """The two revision-proven attempts of one navigation query."""

    def __init__(
        self, navigation: CodeNavigation, request: NavigationRequest, deadline: float
    ) -> None:
        self.navigation = navigation
        self.request = request
        self.deadline = deadline
        self.revision_before = ""

    def run(self) -> NavigationResult:
        for attempt in range(2):
            try:
                return self._attempt(attempt)
            except _Retry:
                continue
            except _Finished as finished:
                return finished.result
        raise AssertionError("navigation attempt loop must return")

    def _empty(
        self,
        status: NavigationStatus,
        revision: str,
        warning: str,
        *,
        with_provider: bool = False,
    ) -> NavigationResult:
        provider = self.navigation.provider if with_provider else None
        provider_version = self.navigation.provider_version if with_provider else None
        return _empty_result(
            self.request,
            status,
            revision_before=revision,
            revision_after=revision,
            provider=provider,
            provider_version=provider_version,
            readiness=self.navigation._session.readiness,
            warnings=(warning,),
        )

    def _attempt(self, attempt: int) -> NavigationResult:
        before = self._revision()
        self.revision_before = before.revision_sha256
        if not self.revision_before:
            raise _Finished(
                self._empty(NavigationStatus.ERROR, "", "workspace revision is unavailable")
            )
        outcome = self._outcome(attempt, before)
        if outcome.status is NavigationStatus.STALE and attempt == 0:
            self.revision_before = outcome.workspace_revision_before
            raise _Retry
        return outcome

    def _revision(self) -> WorkspaceRevision:
        try:
            _check_deadline(self.deadline)
            revision = _compute_revision(self.navigation._repository, deadline=self.deadline)
            _check_deadline(self.deadline)
        except TimeoutError:
            raise _Finished(
                self._empty(
                    NavigationStatus.TIMEOUT,
                    self.revision_before,
                    "revision computation timed out",
                )
            ) from None
        except (OSError, ValueError, RuntimeError):
            raise _Finished(
                self._empty(NavigationStatus.ERROR, "", "revision computation failed")
            ) from None
        return revision

    def _outcome(self, attempt: int, before: WorkspaceRevision) -> NavigationResult:
        try:
            return self.navigation._attempt_query(self.request, before, deadline=self.deadline)
        except TimeoutError:
            raise _Finished(
                self._empty(
                    NavigationStatus.TIMEOUT,
                    self.revision_before,
                    "navigation attempt timed out",
                    with_provider=True,
                )
            ) from None
        except _RevisionMismatch:
            if attempt == 0:
                raise _Retry from None
            raise _Finished(
                self._empty(
                    NavigationStatus.STALE,
                    self.revision_before,
                    "navigation target changed during the request",
                    with_provider=True,
                )
            ) from None


class _QueryAttempt:
    """One navigation attempt pinned to one workspace revision."""

    def __init__(
        self,
        navigation: CodeNavigation,
        request: NavigationRequest,
        before_revision: WorkspaceRevision,
        deadline: float,
    ) -> None:
        self.navigation = navigation
        self.session = navigation._session
        self.request = request
        self.before_revision = before_revision
        self.deadline = deadline
        self.provider = navigation.provider
        self.provider_version = navigation.provider_version
        self.revision_before = before_revision.revision_sha256
        self.provider_failure: NavigationStatus | None = None
        self.validation_warning: str | None = None
        self.anchor: SourceAnchor | None = None
        self.raw_graph_locations: tuple[NavigationLocation, ...] = ()
        self.structural_failed = False
        self.structural_input_truncated = False

    def finish(self, status: NavigationStatus, warning: str, revision_after: str) -> NoReturn:
        raise _Finished(
            _empty_result(
                self.request,
                status,
                revision_before=self.revision_before,
                revision_after=revision_after,
                provider=self.provider,
                provider_version=self.provider_version,
                readiness=self.session.readiness,
                warnings=(warning,),
            )
        )

    def run(self) -> NavigationResult:
        self.provider_failure = self._guarded_setup(
            lambda: self.session.synchronize(self.before_revision, deadline=self.deadline)
        )
        self.revision_entries = _index_revision_entries(
            self.before_revision,
            deadline=self.deadline,
        )
        self.documents = self.navigation._seed_source_documents(self.revision_entries)
        self.expected_source, self.source_document = _load_revision_document(
            self.navigation._repository,
            self.request.path,
            self.revision_entries,
            self.documents,
            deadline=self.deadline,
        )
        self._validate_anchor()
        outcome = self._outcome(self._open_document())
        self._collect_structural()
        return self._result(outcome)

    def _guarded_setup(self, call: Callable[[], object]) -> NavigationStatus | None:
        try:
            _check_deadline(self.deadline)
            call()
            _check_deadline(self.deadline)
        except TimeoutError:
            _check_deadline(self.deadline)
            return NavigationStatus.NOT_READY
        except (OSError, ValueError, RuntimeError):
            _check_deadline(self.deadline)
            return NavigationStatus.NOT_READY
        return None

    def _validate_anchor(self) -> None:
        if self.source_document is None:
            self.validation_warning = "source document validation failed"
            return
        try:
            _check_deadline(self.deadline)
            self.anchor = self.source_document.validate_anchor(
                line=self.request.line,
                character=self.request.character,
            )
            _check_deadline(self.deadline)
        except (ValueError, TypeError):
            self.validation_warning = "anchor validation failed"

    def _ready(self) -> bool:
        return self.validation_warning is None and self.provider_failure is None

    def _open_document(self) -> OpenDocument | None:
        if not self._ready():
            return None
        opened: list[OpenDocument] = []
        self.provider_failure = self._guarded_setup(
            lambda: opened.append(
                self.session.open_document(self.request.path, deadline=self.deadline)
            )
        )
        return opened[0] if opened else None

    def _outcome(self, document: OpenDocument | None) -> _ProviderOutcome:
        if self._ready():
            return self._provider_outcome(document)
        if self.validation_warning is not None:
            return _not_ready_outcome(NavigationStatus.ERROR)
        return _not_ready_outcome(NavigationStatus.NOT_READY)

    def _provider_outcome(self, document: OpenDocument | None) -> _ProviderOutcome:
        if document is None:
            self.finish(
                NavigationStatus.ERROR, "provider document is unavailable", self.revision_before
            )
        if self.expected_source is None or self.source_document is None or self.anchor is None:
            raise AssertionError("validated request source is unavailable")
        self._require_bound(document)
        self.documents[document.source.uri] = self.source_document
        return self._provider_call(document)

    def _require_bound(self, document: OpenDocument) -> None:
        source_entry = self.revision_entries.get(self.request.path)
        if document.source != self.expected_source:
            self.finish(
                NavigationStatus.ERROR, "provider document binding failed", self.revision_before
            )
        _check_deadline(self.deadline)
        source_sha256 = hashlib_sha256(document.content)
        _check_deadline(self.deadline)
        _require_unchanged_entry(source_entry, document, source_sha256)
        if not _entry_binds(source_entry, document, source_sha256):
            self.finish(
                NavigationStatus.ERROR, "source document validation failed", self.revision_before
            )

    def _provider_call(self, document: OpenDocument) -> _ProviderOutcome:
        try:
            _check_deadline(self.deadline)
            outcome = self.navigation._provider_request(
                self.request, self.anchor, document, deadline=self.deadline
            )
            _check_deadline(self.deadline)
        except TimeoutError:
            self.finish(
                NavigationStatus.TIMEOUT, "provider request timed out", self.revision_before
            )
        except (OSError, ValueError, RuntimeError):
            return _ProviderOutcome(
                (),
                (),
                None,
                "not_ready",
                True,
                self.request.capability,
                document.version,
                NavigationStatus.ERROR,
            )
        return outcome

    def _collect_structural(self) -> None:
        candidates = self.navigation._structural_candidates
        if self.validation_warning is not None or candidates is None:
            return
        self._call_structural(candidates)

    def _call_structural(
        self, candidates: Callable[[NavigationRequest, float], Iterable[NavigationLocation]]
    ) -> None:
        try:
            _check_deadline(self.deadline)
            structural_values = candidates(self.request, self.deadline)
            _check_deadline(self.deadline)
            (
                self.raw_graph_locations,
                self.structural_input_truncated,
            ) = _bounded_callback_values(
                structural_values,
                deadline=self.deadline,
            )
            _check_deadline(self.deadline)
        except TimeoutError:
            raise
        except NavigationInterruption:
            raise
        except Exception:
            self.structural_failed = True

    def _provider_facts(
        self, outcome: _ProviderOutcome, encoding: object, lsp_provenance: tuple[Provenance, ...]
    ) -> tuple[tuple[NavigationLocation, ...], bool, tuple[NavigationDiagnostic, ...], bool]:
        if not isinstance(encoding, PositionEncoding):
            return (), bool(outcome.locations), (), bool(outcome.diagnostics)
        locations, partial_locations = _normalize_locations(
            self.navigation._repository,
            outcome.locations,
            resolution=ResolutionLabel.LSP_CONFIRMED,
            provenance=lsp_provenance,
            documents=self.documents,
            revision_entries=self.revision_entries,
            encoding=encoding,
            deadline=self.deadline,
        )
        diagnostics, partial_diagnostics = _normalize_diagnostics(
            self.navigation._repository,
            outcome.diagnostics,
            revision_entries=self.revision_entries,
            documents=self.documents,
            encoding=encoding,
            provenance=lsp_provenance,
            deadline=self.deadline,
        )
        return locations, partial_locations, diagnostics, partial_diagnostics

    def _hover_filtered(self, outcome: _ProviderOutcome, encoding: object) -> bool:
        if outcome.hover is None or outcome.hover.range is None:
            return False
        if not isinstance(encoding, PositionEncoding) or self.source_document is None:
            return True
        self.documents.consume_document(self.source_document)
        return not _hover_range_valid(
            self.source_document, outcome.hover.range, encoding, self.deadline
        )

    def _graph_locations(self) -> tuple[tuple[NavigationLocation, ...], bool]:
        return _normalize_structural_locations(
            self.navigation._repository,
            self.raw_graph_locations,
            revision_entries=self.revision_entries,
            documents=self.documents,
            provenance=_graph_provenance("graph_candidate"),
            deadline=self.deadline,
        )

    def _result(self, outcome: _ProviderOutcome) -> NavigationResult:
        _check_deadline(self.deadline)
        encoding = self.session.position_encoding
        _check_deadline(self.deadline)
        lsp_provenance = (
            Provenance(
                source="lsp",
                provider=self.provider,
                version=self.provider_version or "",
                observation="provider_reported",
            ),
        )
        locations, partial_locations, diagnostics, partial_diagnostics = self._provider_facts(
            outcome, encoding, lsp_provenance
        )
        partial_hover = self._hover_filtered(outcome, encoding)
        graph_locations, partial_graph = self._graph_locations()
        if outcome.references_require_graph:
            locations = _within_graph_spans(locations, graph_locations)
        warnings = (
            *_provider_warnings(self.provider_failure, outcome),
            *_flagged((
                (partial_locations, "provider locations partially filtered"),
                (partial_diagnostics, "provider diagnostics partially filtered"),
                (partial_hover, "provider hover range was filtered"),
                (self.structural_failed, "structural fallback failed"),
                (self.structural_input_truncated, "structural callback input bound reached"),
                (partial_graph, "structural candidates partially filtered"),
            )),
        )
        merged, truncated = _merge_locations(locations, graph_locations, deadline=self.deadline)
        _check_deadline(self.deadline)
        merged, over_budget = _fit_fact_budget(merged, diagnostics)
        graph_only = any(
            location.resolution is ResolutionLabel.GRAPH_CANDIDATE for location in merged
        )
        warnings = (
            *warnings,
            *_flagged((
                (graph_only, "structural fallback appended"),
                (truncated or over_budget, "result fact limit reached"),
            )),
        )
        provenance = self._used_provenance(outcome, merged, diagnostics, lsp_provenance)
        return self._proven_result(outcome, encoding, merged, diagnostics, provenance, warnings)

    @staticmethod
    def _used_provenance(
        outcome: _ProviderOutcome,
        merged: tuple[NavigationLocation, ...],
        diagnostics: tuple[NavigationDiagnostic, ...],
        lsp_provenance: tuple[Provenance, ...],
    ) -> tuple[Provenance, ...]:
        used = [*_fact_provenance(merged), *_fact_provenance(diagnostics)]
        if _provider_reported(outcome):
            used.extend(lsp_provenance)
        return _union_provenance(tuple(used))

    def _revision_after(self) -> str:
        _check_deadline(self.deadline)
        try:
            _check_deadline(self.deadline)
            after_revision = _compute_post_revision(
                self.navigation._repository,
                self.before_revision,
                deadline=self.deadline,
            )
            _check_deadline(self.deadline)
        except TimeoutError:
            self.finish(
                NavigationStatus.TIMEOUT, "post-request revision timed out", self.revision_before
            )
        except (OSError, ValueError, RuntimeError):
            self.finish(
                NavigationStatus.ERROR, "post-request revision failed", self.revision_before
            )
        return after_revision.revision_sha256

    def _proven_revision_after(self) -> str:
        """The unchanged post-request revision of a request that validated."""
        revision_after = self._revision_after()
        if revision_after != self.revision_before:
            self.finish(
                NavigationStatus.STALE, "workspace changed across the request", revision_after
            )
        if self.validation_warning is not None:
            self.finish(NavigationStatus.ERROR, self.validation_warning, revision_after)
        return revision_after

    def _proven_result(
        self,
        outcome: _ProviderOutcome,
        encoding: object,
        merged: tuple[NavigationLocation, ...],
        diagnostics: tuple[NavigationDiagnostic, ...],
        provenance: tuple[Provenance, ...],
        warnings: tuple[str, ...],
    ) -> NavigationResult:
        revision_after = self._proven_revision_after()
        useful_structural = any(_structural_provenance(location) for location in merged)
        status = _query_status(outcome, useful_structural, warnings)
        result = NavigationResult(
            status=status,
            requested_capability=self.request.capability,
            effective_capability=outcome.effective_capability,
            provider=self.provider,
            provider_version=self.provider_version,
            repository_id=self.navigation._repository.repository_id,
            checkout_id=self.navigation._repository.checkout_id,
            workspace_revision_before=self.revision_before,
            workspace_revision_after=revision_after,
            document_version=outcome.document_version,
            position_encoding=_known_encoding(encoding),
            readiness=self.session.readiness,
            symbol=None,
            total=self._total(merged, diagnostics),
            offset=self.request.offset,
            limit=self.request.limit,
            locations=merged,
            diagnostics=diagnostics,
            hover=_hover_contents(outcome),
            resolution=_status_resolution(status, merged),
            provenance=provenance,
            warnings=warnings,
        )
        _check_deadline(self.deadline)
        if result.status in {NavigationStatus.OK, NavigationStatus.PARTIAL}:
            self.navigation._publish_source_documents(self.revision_entries, self.documents)
        return result

    def _total(
        self,
        merged: tuple[NavigationLocation, ...],
        diagnostics: tuple[NavigationDiagnostic, ...],
    ) -> int:
        if self.request.capability is Capability.DIAGNOSTICS:
            return len(diagnostics)
        return len(merged)


def _known_encoding(encoding: object) -> PositionEncoding | None:
    return encoding if isinstance(encoding, PositionEncoding) else None


def _hover_contents(outcome: _ProviderOutcome) -> str | None:
    return outcome.hover.contents if outcome.hover is not None else None


def _status_resolution(
    status: NavigationStatus, merged: tuple[NavigationLocation, ...]
) -> ResolutionLabel:
    if status is NavigationStatus.UNSUPPORTED:
        return ResolutionLabel.UNSUPPORTED
    return _merged_resolution(merged)


class _StructuralRun:
    """Two revision-proven attempts of one symbol resolution or edge verification."""

    def __init__(
        self,
        navigation: CodeNavigation,
        repository: RepositoryScope,
        capability: Capability,
        symbol: str | None,
        deadline: float | None,
        messages: Mapping[str, str],
    ) -> None:
        self.navigation = navigation
        self.repository = repository
        self.capability = capability
        self.symbol = symbol
        self.deadline = deadline
        self.messages = messages
        self.prior_revision = ""
        self.attempt = 0
        self.before: WorkspaceRevision | None = None
        self.revision_before = ""
        self.revision_after = ""
        self.revision_entries: dict[str, RevisionEntry] = {}

    def result(
        self,
        status: NavigationStatus,
        revision_before: str,
        revision_after: str,
        **fields: object,
    ) -> NavigationResult:
        return _structural_result(
            self.repository,
            status=status,
            capability=self.capability,
            revision_before=revision_before,
            revision_after=revision_after,
            readiness=self.navigation._session.readiness,
            symbol=self.symbol,
            **fields,
        )

    def finish(
        self,
        status: NavigationStatus,
        warning: str,
        revision_before: str,
        revision_after: str,
    ) -> NoReturn:
        raise _Finished(
            self.result(status, revision_before, revision_after, warnings=(warning,))
        )

    def run(self, body: Callable[[_StructuralRun], NavigationResult]) -> NavigationResult:
        for attempt in range(2):
            self.attempt = attempt
            try:
                return self._attempt(body)
            except _Retry:
                continue
            except _Finished as finished:
                return finished.result
        raise AssertionError(self.messages["loop"])

    def _attempt(self, body: Callable[[_StructuralRun], NavigationResult]) -> NavigationResult:
        self._revision()
        self._index()
        return body(self)

    def _revision(self) -> None:
        prior = self.prior_revision
        try:
            _check_deadline(self.deadline)
            self.before = _compute_revision(self.navigation._repository, deadline=self.deadline)
            _check_deadline(self.deadline)
        except TimeoutError:
            self.finish(NavigationStatus.TIMEOUT, self.messages["revision_timeout"], prior, prior)
        except (OSError, ValueError, RuntimeError):
            self.finish(NavigationStatus.ERROR, self.messages["revision_failed"], prior, prior)
        self.revision_before = self.before.revision_sha256
        if not self.revision_before:
            self.finish(NavigationStatus.ERROR, self.messages["revision_unavailable"], "", "")

    def _index(self) -> None:
        try:
            self.revision_entries = _index_revision_entries(self.before, deadline=self.deadline)
        except TimeoutError:
            self.finish(
                NavigationStatus.TIMEOUT,
                self.messages["indexing_timeout"],
                self.revision_before,
                self.revision_before,
            )

    def changed(self, warning: str, revision_after: str) -> NoReturn:
        """The workspace moved: the first attempt retries, the second reports it stale."""
        if self.attempt == 0:
            self.prior_revision = self.revision_before
            raise _Retry
        self.finish(NavigationStatus.STALE, warning, self.revision_before, revision_after)

    def prove_unchanged(self) -> None:
        before = self.revision_before
        try:
            _check_deadline(self.deadline)
            after = _compute_post_revision(
                self.navigation._repository,
                self.before,
                deadline=self.deadline,
            )
            _check_deadline(self.deadline)
        except TimeoutError:
            self.finish(NavigationStatus.TIMEOUT, self.messages["post_timeout"], before, before)
        except (OSError, ValueError, RuntimeError):
            self.finish(NavigationStatus.ERROR, self.messages["post_failed"], before, before)
        self.revision_after = after.revision_sha256
        if self.revision_after != before:
            self.changed(self.messages["changed"], self.revision_after)

    def require_constructed(self) -> None:
        try:
            _check_deadline(self.deadline)
        except TimeoutError:
            self.finish(
                NavigationStatus.TIMEOUT,
                self.messages["construction_timeout"],
                self.revision_before,
                self.revision_after,
            )


def _symbol_candidates(
    run: _StructuralRun,
    raw_candidates: tuple[NavigationLocation, ...],
    documents: _AttemptDocuments,
) -> tuple[tuple[NavigationLocation, ...], bool]:
    try:
        return _normalize_structural_locations(
            run.repository,
            raw_candidates,
            revision_entries=run.revision_entries,
            documents=documents,
            provenance=_graph_provenance("name_resolution"),
            deadline=run.deadline,
        )
    except TimeoutError:
        run.finish(
            NavigationStatus.TIMEOUT,
            "symbol normalization timed out",
            run.revision_before,
            run.revision_before,
        )
    except _RevisionMismatch:
        run.changed("symbol target changed during resolution", run.revision_before)


def _labeled_candidates(
    candidates: tuple[NavigationLocation, ...], input_truncated: bool
) -> tuple[tuple[NavigationLocation, ...], ResolutionLabel]:
    if len(candidates) == 1 and not input_truncated:
        confirmed = replace(candidates[0], resolution=ResolutionLabel.GRAPH_CONFIRMED)
        return (confirmed,), ResolutionLabel.GRAPH_CONFIRMED
    if candidates:
        ambiguous = tuple(
            replace(location, resolution=ResolutionLabel.AMBIGUOUS) for location in candidates
        )
        return ambiguous, ResolutionLabel.AMBIGUOUS
    return candidates, ResolutionLabel.UNRESOLVED


def _symbol_warnings(
    resolver_failure: NavigationStatus | None,
    filtered: bool,
    input_truncated: bool,
    truncated: bool,
    candidates: tuple[NavigationLocation, ...],
) -> tuple[str, ...]:
    none_found = not candidates and resolver_failure is None
    return _flagged((
        (resolver_failure is NavigationStatus.TIMEOUT, "symbol resolver timed out"),
        (resolver_failure is NavigationStatus.ERROR, "symbol resolver failed"),
        (filtered, "symbol candidates partially filtered"),
        (input_truncated, "symbol callback input bound reached"),
        (truncated, "symbol candidate limit reached"),
        (len(candidates) > 1, "multiple declarations require disambiguation"),
        (none_found, "no structural candidates"),
    ))


def _symbol_status(
    resolver_failure: NavigationStatus | None,
    candidates: tuple[NavigationLocation, ...],
    warnings: tuple[str, ...],
) -> NavigationStatus:
    if resolver_failure is not None and not candidates:
        return resolver_failure
    if len(candidates) == 1 and not warnings:
        return NavigationStatus.OK
    return NavigationStatus.PARTIAL


def _edge_anchors_valid(
    run: _StructuralRun,
    source: SourceAnchor,
    target: SourceAnchor,
    documents: _AttemptDocuments,
) -> bool:
    try:
        source_valid = _validate_revision_anchor(
            run.repository, source, run.revision_entries, documents, deadline=run.deadline
        )
        target_valid = _validate_revision_anchor(
            run.repository, target, run.revision_entries, documents, deadline=run.deadline
        )
    except TimeoutError:
        run.finish(
            NavigationStatus.TIMEOUT,
            "edge anchor validation timed out",
            run.revision_before,
            run.revision_before,
        )
    except _RevisionMismatch:
        run.changed("edge target changed during verification", run.revision_before)
    return source_valid and target_valid


def _require_edge_proof(
    run: _StructuralRun, anchors_valid: bool, verifier_failure: NavigationStatus | None
) -> None:
    if not anchors_valid:
        run.finish(
            NavigationStatus.ERROR,
            "edge anchor validation failed",
            run.revision_before,
            run.revision_after,
        )
    if verifier_failure is not None:
        run.finish(
            verifier_failure,
            _VERIFIER_WARNINGS[verifier_failure],
            run.revision_before,
            run.revision_after,
        )
