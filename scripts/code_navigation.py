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
from typing import TypeVar

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
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    if nonempty and not value:
        raise ValueError(f"{label} must not be empty")


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
        if not isinstance(self.range, PositionRange):
            raise TypeError("range must be a PositionRange")
        _require_integer(self.line, "line", minimum=1)
        _require_integer(self.character, "character", minimum=0)
        _require_text(self.containing_symbol, "containing_symbol", optional=True)
        _require_text(self.signature, "signature", optional=True)
        if not isinstance(self.resolution, ResolutionLabel):
            raise TypeError("resolution must be a ResolutionLabel")
        provenance = tuple(self.provenance)
        if any(not isinstance(item, Provenance) for item in provenance):
            raise TypeError("provenance must contain Provenance records")
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
        if not isinstance(self.range, PositionRange):
            raise TypeError("range must be a PositionRange")
        if not isinstance(self.severity, DiagnosticSeverity):
            raise TypeError("severity must be a DiagnosticSeverity")
        _require_text(self.code, "code", optional=True)
        _require_text(self.message, "message")
        related = tuple(self.related)
        if any(not isinstance(item, NavigationLocation) for item in related):
            raise TypeError("related must contain NavigationLocation records")
        provenance = tuple(self.provenance)
        if any(not isinstance(item, Provenance) for item in provenance):
            raise TypeError("provenance must contain Provenance records")
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
        if not isinstance(self.repository, RepositoryScope):
            raise TypeError("repository must be a RepositoryScope")
        if not isinstance(self.capability, Capability):
            raise TypeError("capability must be a Capability")
        _require_relative_path(self.path, "path")
        _require_integer(self.line, "line", minimum=1)
        _require_integer(self.character, "character", minimum=0)
        _require_integer(self.offset, "offset", minimum=0)
        _require_integer(self.limit, "limit", minimum=1)
        if self.limit > 100:
            raise ValueError("limit must be between 1 and 100")
        _require_text(self.direction, "direction", optional=True)
        if self.direction is not None and self.direction not in {"incoming", "outgoing"}:
            raise ValueError("direction must be None, 'incoming', or 'outgoing'")
        if self.capability is Capability.CALLS:
            if self.direction is None:
                raise ValueError("Capability.CALLS requires a direction")
        elif self.direction is not None:
            raise ValueError("direction must be None unless capability is CALLS")


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
        if not isinstance(self.status, NavigationStatus):
            raise TypeError("status must be a NavigationStatus")
        if not isinstance(self.requested_capability, Capability):
            raise TypeError("requested_capability must be a Capability")
        if self.effective_capability is not None and not isinstance(
            self.effective_capability, Capability
        ):
            raise TypeError("effective_capability must be a Capability or None")
        _require_text(self.provider, "provider", optional=True)
        _require_text(self.provider_version, "provider_version", optional=True)
        _require_text(self.repository_id, "repository_id", nonempty=True)
        _require_text(self.checkout_id, "checkout_id", nonempty=True)
        _require_text(self.workspace_revision_before, "workspace_revision_before")
        _require_text(self.workspace_revision_after, "workspace_revision_after")
        if self.document_version is not None:
            _require_integer(self.document_version, "document_version", minimum=0)
        if self.position_encoding is not None and not isinstance(
            self.position_encoding, PositionEncoding
        ):
            raise TypeError("position_encoding must be a PositionEncoding or None")
        _require_text(self.readiness, "readiness", nonempty=True)
        _require_text(self.symbol, "symbol", optional=True)
        _require_integer(self.total, "total", minimum=0)
        _require_integer(self.offset, "offset", minimum=0)
        _require_integer(self.limit, "limit", minimum=1)
        if self.limit > 100:
            raise ValueError("limit must be between 1 and 100")
        locations = tuple(self.locations)
        if any(not isinstance(item, NavigationLocation) for item in locations):
            raise TypeError("locations must contain NavigationLocation records")
        diagnostics = tuple(self.diagnostics)
        if any(not isinstance(item, NavigationDiagnostic) for item in diagnostics):
            raise TypeError("diagnostics must contain NavigationDiagnostic records")
        _require_text(self.hover, "hover", optional=True)
        if not isinstance(self.resolution, ResolutionLabel):
            raise TypeError("resolution must be a ResolutionLabel")
        provenance = tuple(self.provenance)
        if any(not isinstance(item, Provenance) for item in provenance):
            raise TypeError("provenance must contain Provenance records")
        warnings = tuple(self.warnings)
        if any(not isinstance(item, str) for item in warnings):
            raise TypeError("warnings must contain strings")
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
    normalized: list[NavigationLocation] = []
    partial = False
    _check_deadline(deadline)
    for location in locations:
        _check_deadline(deadline)
        if not isinstance(location, LspLocation):
            partial = True
            continue
        _check_deadline(deadline)
        if isinstance(documents, _AttemptDocuments):
            source = documents.normalize_provider_source(repository, location.uri)
        else:
            source = normalize_provider_uri(repository, location.uri)
        _check_deadline(deadline)
        if source is None:
            partial = True
            continue
        document = _load_revision_source(
            source,
            revision_entries,
            documents,
            deadline=deadline,
        )
        _check_deadline(deadline)
        if document is None:
            partial = True
            continue
        _check_deadline(deadline)
        range_ = _range_from_lsp(location, document, encoding)
        _check_deadline(deadline)
        if range_ is None:
            partial = True
            continue
        line = location.range.start.line + 1
        line_start, _line_end = document.line_spans[location.range.start.line]
        character = range_.byte_start - line_start
        normalized.append(
            NavigationLocation(
                path=source.relative_path,
                range=range_,
                line=line,
                character=character,
                containing_symbol=None,
                signature=None,
                resolution=resolution,
                provenance=provenance,
            )
        )
        _check_deadline(deadline)
    _check_deadline(deadline)
    return tuple(normalized), partial


def _load_revision_source(
    source: RepositorySource,
    revision_entries: Mapping[str, RevisionEntry],
    documents: dict[str, SourceDocument | None],
    *,
    deadline: float | None,
) -> SourceDocument | None:
    _check_deadline(deadline)
    if source.uri in documents:
        document = documents[source.uri]
        if isinstance(documents, _AttemptDocuments) and document is not None:
            documents.consume(source.uri, document)
        return document
    entry = revision_entries.get(source.relative_path)
    if (
        not isinstance(entry, RevisionEntry)
        or entry.sha256 is None
        or isinstance(entry.size, bool)
        or not isinstance(entry.size, int)
        or entry.size < 0
        or entry.size > _MAX_NAVIGATION_SOURCE_BYTES
    ):
        documents[source.uri] = None
        return None
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
    try:
        _check_deadline(deadline)
        document = SourceDocument.from_bytes(source.relative_path, content)
        _check_deadline(deadline)
    except (UnicodeError, ValueError, RuntimeError, TypeError):
        document = None
    documents[source.uri] = document
    if isinstance(documents, _AttemptDocuments) and document is not None:
        documents.consume(source.uri, document)
    return document


def _byte_position(
    document: SourceDocument,
    byte_offset: int,
) -> tuple[int, int] | None:
    if (
        isinstance(byte_offset, bool)
        or not isinstance(byte_offset, int)
        or byte_offset < 0
        or byte_offset > len(document.content)
    ):
        return None
    try:
        document.content[:byte_offset].decode("utf-8", errors="strict")
    except UnicodeError:
        return None
    for index, (start, end) in enumerate(document.line_spans):
        if start <= byte_offset <= end:
            return index + 1, byte_offset - start
    return None


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
        if not isinstance(location, NavigationLocation):
            partial = True
            continue
        path_key = f"path:{location.path}"
        if path_key in documents:
            document = documents[path_key]
            if isinstance(documents, _AttemptDocuments) and document is not None:
                documents.consume_document(document)
        else:
            try:
                _check_deadline(deadline)
                source = resolve_repository_source(repository, location.path)
                _check_deadline(deadline)
            except TimeoutError:
                raise
            except (OSError, UnicodeError, ValueError, RuntimeError, TypeError):
                documents[path_key] = None
                partial = True
                continue
            document = _load_revision_source(
                source,
                revision_entries,
                documents,
                deadline=deadline,
            )
            documents[path_key] = document
        if document is None or location.range.byte_end > len(document.content):
            partial = True
            continue
        _check_deadline(deadline)
        start = _byte_position(document, location.range.byte_start)
        _check_deadline(deadline)
        end = _byte_position(document, location.range.byte_end)
        _check_deadline(deadline)
        if start is None or end is None:
            partial = True
            continue
        normalized.append(
            NavigationLocation(
                document.path,
                location.range,
                start[0],
                start[1],
                location.containing_symbol,
                location.signature,
                ResolutionLabel.GRAPH_CANDIDATE,
                _union_provenance(location.provenance, provenance),
            )
        )
        _check_deadline(deadline)
    _check_deadline(deadline)
    return tuple(normalized), partial


def _load_revision_document(
    repository: RepositoryScope,
    path: str,
    revision_entries: Mapping[str, RevisionEntry],
    documents: dict[str, SourceDocument | None],
    *,
    deadline: float | None,
) -> tuple[RepositorySource | None, SourceDocument | None]:
    entry = revision_entries.get(path)
    recorded_source = (
        isinstance(entry, RevisionEntry)
        and entry.sha256 is not None
        and not isinstance(entry.size, bool)
        and isinstance(entry.size, int)
        and 0 <= entry.size <= _MAX_NAVIGATION_SOURCE_BYTES
    )
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
    normalized: list[NavigationDiagnostic] = []
    partial = len(diagnostics) > _MAX_NAVIGATION_FACTS
    fact_count = 0
    for diagnostic in diagnostics[:_MAX_NAVIGATION_FACTS]:
        _check_deadline(deadline)
        if fact_count >= _MAX_NAVIGATION_FACTS:
            partial = True
            break
        if not isinstance(diagnostic, LspDiagnostic):
            partial = True
            continue
        severity = (
            None
            if isinstance(diagnostic.severity, bool)
            else _DIAGNOSTIC_SEVERITIES.get(diagnostic.severity)
        )
        if (
            severity is None
            or (diagnostic.code is not None and not isinstance(diagnostic.code, str))
            or not isinstance(diagnostic.message, str)
        ):
            partial = True
            continue
        locations, filtered = _normalize_locations(
            repository,
            (LspLocation(diagnostic.uri, diagnostic.range),),
            resolution=ResolutionLabel.LSP_CONFIRMED,
            provenance=provenance,
            documents=documents,
            revision_entries=revision_entries,
            encoding=encoding,
            deadline=deadline,
        )
        if filtered or len(locations) != 1:
            partial = True
            continue
        related: list[NavigationLocation] = []
        remaining_related = _MAX_NAVIGATION_FACTS - fact_count - 1
        if len(diagnostic.related) > remaining_related:
            partial = True
        for value in diagnostic.related[:remaining_related]:
            _check_deadline(deadline)
            if (
                not isinstance(value, tuple)
                or len(value) != 2
                or not isinstance(value[0], LspLocation)
            ):
                partial = True
                continue
            related_locations, related_filtered = _normalize_locations(
                repository,
                (value[0],),
                resolution=ResolutionLabel.LSP_CONFIRMED,
                provenance=provenance,
                documents=documents,
                revision_entries=revision_entries,
                encoding=encoding,
                deadline=deadline,
            )
            if related_filtered or len(related_locations) != 1:
                partial = True
                continue
            related.append(related_locations[0])
            _check_deadline(deadline)
        location = locations[0]
        normalized.append(
            NavigationDiagnostic(
                location.path,
                location.range,
                severity,
                diagnostic.code,
                diagnostic.message,
                _dedupe_locations(tuple(related), deadline=deadline),
                provenance,
            )
        )
        fact_count += 1 + len(related)
        _check_deadline(deadline)
    _check_deadline(deadline)
    unique = {_diagnostic_key(value): value for value in normalized}
    _check_deadline(deadline)
    ordered = tuple(unique[key] for key in sorted(unique))
    _check_deadline(deadline)
    return ordered, partial


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
        key = (location.path, location.range.byte_start, location.range.byte_end)
        graph_by_span.setdefault(key, []).append(location)
    matched: set[tuple[str, int, int]] = set()
    confirmed: list[NavigationLocation] = []
    for location in lsp:
        _check_deadline(deadline)
        key = (location.path, location.range.byte_start, location.range.byte_end)
        matches = graph_by_span.get(key, ())
        if matches:
            matched.add(key)
            confirmed.append(
                replace(
                    location,
                    resolution=ResolutionLabel.LSP_AND_GRAPH,
                    provenance=_union_provenance(
                        location.provenance,
                        *(match.provenance for match in matches),
                    ),
                )
            )
        else:
            confirmed.append(location)
        _check_deadline(deadline)
    _check_deadline(deadline)
    candidates = [
        replace(location, resolution=ResolutionLabel.GRAPH_CANDIDATE)
        for location in graph
        if (location.path, location.range.byte_start, location.range.byte_end)
        not in matched
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
        if not isinstance(repository, RepositoryScope):
            raise TypeError("repository must be a RepositoryScope")
        if not isinstance(session, PyrightSession):
            raise TypeError("session must be a PyrightSession")
        if not isinstance(identity, PyrightIdentity):
            raise TypeError("identity must be a PyrightIdentity")
        if identity != session.identity:
            raise ValueError("identity must match the Pyright session identity")
        session_repository = session._repository
        if (
            repository.repository_id != session_repository.repository_id
            or repository.checkout_id != session_repository.checkout_id
        ):
            raise ValueError("repository must match the Pyright session repository")
        if structural_candidates is not None and not callable(structural_candidates):
            raise TypeError("structural_candidates must be callable or None")
        if symbol_resolver is not None and not callable(symbol_resolver):
            raise TypeError("symbol_resolver must be callable or None")
        if edge_verifier is not None and not callable(edge_verifier):
            raise TypeError("edge_verifier must be callable or None")
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
        if (
            not isinstance(uri, str)
            or not uri.startswith("file:")
            or not isinstance(entry, RevisionEntry)
            or entry.path != document.path
            or not isinstance(entry.sha256, str)
            or entry.sha256 != document.source_sha256
            or isinstance(entry.size, bool)
            or not isinstance(entry.size, int)
            or entry.size < 0
            or entry.size > _MAX_NAVIGATION_SOURCE_BYTES
            or entry.size != len(document.content)
        ):
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
        candidates: dict[tuple[str, str, int], SourceDocument] = {}
        for uri, document in documents.consumed.items():
            key = self._source_document_cache_key(uri, document, revision_entries)
            if key is None:
                continue
            if (
                self._source_document_retained_bytes(key, document)
                > _MAX_SOURCE_DOCUMENT_CACHE_BYTES
            ):
                continue
            candidates[key] = document
        if not candidates:
            return
        with self._lock:
            if self._closed:
                return
            for key, document in candidates.items():
                previous = self._source_document_cache.get(key)
                if previous is document:
                    self._source_document_cache.move_to_end(key)
                    continue
                if previous is not None:
                    self._source_document_cache.pop(key)
                    self._source_document_cache_bytes -= (
                        self._source_document_retained_bytes(key, previous)
                    )
                self._source_document_cache[key] = document
                self._source_document_cache_bytes += (
                    self._source_document_retained_bytes(key, document)
                )
                while (
                    len(self._source_document_cache)
                    > _MAX_SOURCE_DOCUMENT_CACHE_ENTRIES
                    or self._source_document_cache_bytes
                    > _MAX_SOURCE_DOCUMENT_CACHE_BYTES
                ):
                    old_key, old_document = self._source_document_cache.popitem(
                        last=False
                    )
                    self._source_document_cache_bytes -= (
                        self._source_document_retained_bytes(old_key, old_document)
                    )

    def query(
        self,
        request: NavigationRequest,
        *,
        deadline: float,
    ) -> NavigationResult:
        if not isinstance(request, NavigationRequest):
            raise TypeError("request must be a NavigationRequest")
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
            raise TypeError("deadline must be a monotonic timestamp")
        if not math.isfinite(deadline):
            raise ValueError("deadline must be finite")
        if request.repository.checkout_id != self._repository.checkout_id:
            raise ValueError("request must target this navigation repository")
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
        revision_before = ""
        for attempt in range(2):
            try:
                _check_deadline(deadline)
                revision_before_revision = _compute_revision(
                    self._repository, deadline=deadline
                )
                _check_deadline(deadline)
            except TimeoutError:
                return _empty_result(
                    request,
                    NavigationStatus.TIMEOUT,
                    revision_before=revision_before,
                    revision_after=revision_before,
                    readiness=self._session.readiness,
                    warnings=("revision computation timed out",),
                )
            except (OSError, ValueError, RuntimeError):
                return _empty_result(
                    request,
                    NavigationStatus.ERROR,
                    revision_before="",
                    revision_after="",
                    readiness=self._session.readiness,
                    warnings=("revision computation failed",),
                )
            revision_before = revision_before_revision.revision_sha256
            if not revision_before:
                return _empty_result(
                    request,
                    NavigationStatus.ERROR,
                    revision_before="",
                    revision_after="",
                    readiness=self._session.readiness,
                    warnings=("workspace revision is unavailable",),
                )
            try:
                outcome = self._attempt_query(
                    request,
                    revision_before_revision,
                    deadline=deadline,
                )
            except TimeoutError:
                return _empty_result(
                    request,
                    NavigationStatus.TIMEOUT,
                    revision_before=revision_before,
                    revision_after=revision_before,
                    provider=self.provider,
                    provider_version=self.provider_version,
                    readiness=self._session.readiness,
                    warnings=("navigation attempt timed out",),
                )
            except _RevisionMismatch:
                if attempt == 0:
                    continue
                return _empty_result(
                    request,
                    NavigationStatus.STALE,
                    revision_before=revision_before,
                    revision_after=revision_before,
                    provider=self.provider,
                    provider_version=self.provider_version,
                    readiness=self._session.readiness,
                    warnings=("navigation target changed during the request",),
                )
            if outcome.status is not NavigationStatus.STALE or attempt == 1:
                return outcome
            revision_before = outcome.workspace_revision_before
        raise AssertionError("navigation attempt loop must return")

    def _attempt_query(
        self,
        request: NavigationRequest,
        before_revision: WorkspaceRevision,
        *,
        deadline: float,
    ) -> NavigationResult:
        provider = self.provider
        provider_version = self.provider_version
        revision_before = before_revision.revision_sha256
        provider_failure: NavigationStatus | None = None
        try:
            _check_deadline(deadline)
            self._session.synchronize(before_revision, deadline=deadline)
            _check_deadline(deadline)
        except TimeoutError:
            _check_deadline(deadline)
            provider_failure = NavigationStatus.NOT_READY
        except (OSError, ValueError, RuntimeError):
            _check_deadline(deadline)
            provider_failure = NavigationStatus.NOT_READY
        revision_entries = _index_revision_entries(
            before_revision,
            deadline=deadline,
        )
        documents = self._seed_source_documents(revision_entries)
        expected_source, source_document = _load_revision_document(
            self._repository,
            request.path,
            revision_entries,
            documents,
            deadline=deadline,
        )
        request_validation_warning: str | None = None
        anchor: SourceAnchor | None = None
        if source_document is None:
            request_validation_warning = "source document validation failed"
        else:
            try:
                _check_deadline(deadline)
                anchor = source_document.validate_anchor(
                    line=request.line,
                    character=request.character,
                )
                _check_deadline(deadline)
            except (ValueError, TypeError):
                request_validation_warning = "anchor validation failed"
        document: OpenDocument | None = None
        if request_validation_warning is None and provider_failure is None:
            try:
                _check_deadline(deadline)
                document = self._session.open_document(request.path, deadline=deadline)
                _check_deadline(deadline)
            except TimeoutError:
                _check_deadline(deadline)
                provider_failure = NavigationStatus.NOT_READY
            except (OSError, ValueError, RuntimeError):
                _check_deadline(deadline)
                provider_failure = NavigationStatus.NOT_READY
        if request_validation_warning is None and provider_failure is None:
            if document is None:
                return _empty_result(
                    request,
                    NavigationStatus.ERROR,
                    revision_before=revision_before,
                    revision_after=revision_before,
                    provider=provider,
                    provider_version=provider_version,
                    readiness=self._session.readiness,
                    warnings=("provider document is unavailable",),
                )
            if expected_source is None or source_document is None or anchor is None:
                raise AssertionError("validated request source is unavailable")
            source_entry = revision_entries.get(request.path)
            if document.source != expected_source:
                return _empty_result(
                    request,
                    NavigationStatus.ERROR,
                    revision_before=revision_before,
                    revision_after=revision_before,
                    provider=provider,
                    provider_version=provider_version,
                    readiness=self._session.readiness,
                    warnings=("provider document binding failed",),
                )
            _check_deadline(deadline)
            source_sha256 = hashlib_sha256(document.content)
            _check_deadline(deadline)
            if (
                source_entry is not None
                and source_entry.sha256 is not None
                and (
                    source_entry.size != len(document.content)
                    or source_entry.sha256 != source_sha256
                )
            ):
                raise _RevisionMismatch
            if (
                source_entry is None
                or source_entry.sha256 is None
                or source_entry.size != len(document.content)
                or document.source_sha256 != source_sha256
            ):
                return _empty_result(
                    request,
                    NavigationStatus.ERROR,
                    revision_before=revision_before,
                    revision_after=revision_before,
                    provider=provider,
                    provider_version=provider_version,
                    readiness=self._session.readiness,
                    warnings=("source document validation failed",),
                )
            documents[document.source.uri] = source_document
            try:
                _check_deadline(deadline)
                outcome = self._provider_request(
                    request, anchor, document, deadline=deadline
                )
                _check_deadline(deadline)
            except TimeoutError:
                return _empty_result(
                    request,
                    NavigationStatus.TIMEOUT,
                    revision_before=revision_before,
                    revision_after=revision_before,
                    provider=provider,
                    provider_version=provider_version,
                    readiness=self._session.readiness,
                    warnings=("provider request timed out",),
                )
            except (OSError, ValueError, RuntimeError):
                outcome = _ProviderOutcome(
                    (),
                    (),
                    None,
                    "not_ready",
                    True,
                    request.capability,
                    document.version,
                    NavigationStatus.ERROR,
                )
        elif request_validation_warning is not None:
            outcome = _ProviderOutcome(
                (),
                (),
                None,
                "not_ready",
                True,
                None,
                None,
                NavigationStatus.ERROR,
            )
        else:
            outcome = _ProviderOutcome(
                (),
                (),
                None,
                "not_ready",
                True,
                None,
                None,
                NavigationStatus.NOT_READY,
            )
        raw_graph_locations: tuple[NavigationLocation, ...] = ()
        structural_failed = False
        structural_input_truncated = False
        if (
            request_validation_warning is None
            and self._structural_candidates is not None
        ):
            try:
                _check_deadline(deadline)
                structural_values = self._structural_candidates(request, deadline)
                _check_deadline(deadline)
                (
                    raw_graph_locations,
                    structural_input_truncated,
                ) = _bounded_callback_values(
                    structural_values,
                    deadline=deadline,
                )
                _check_deadline(deadline)
            except TimeoutError:
                raise
            except NavigationInterruption:
                raise
            except Exception:
                structural_failed = True
        _check_deadline(deadline)
        encoding = self._session.position_encoding
        _check_deadline(deadline)
        lsp_provenance = (
            Provenance(
                source="lsp",
                provider=provider,
                version=provider_version or "",
                observation="provider_reported",
            ),
        )
        if isinstance(encoding, PositionEncoding):
            normalized_locations, partial_locations = _normalize_locations(
                self._repository,
                outcome.locations,
                resolution=ResolutionLabel.LSP_CONFIRMED,
                provenance=lsp_provenance,
                documents=documents,
                revision_entries=revision_entries,
                encoding=encoding,
                deadline=deadline,
            )
            normalized_diagnostics, partial_diagnostics = _normalize_diagnostics(
                self._repository,
                outcome.diagnostics,
                revision_entries=revision_entries,
                documents=documents,
                encoding=encoding,
                provenance=lsp_provenance,
                deadline=deadline,
            )
        else:
            normalized_locations = ()
            partial_locations = bool(outcome.locations)
            normalized_diagnostics = ()
            partial_diagnostics = bool(outcome.diagnostics)
        partial_hover = False
        if outcome.hover is not None and outcome.hover.range is not None:
            if (
                not isinstance(encoding, PositionEncoding)
                or source_document is None
            ):
                partial_hover = True
            else:
                documents.consume_document(source_document)
                try:
                    _check_deadline(deadline)
                    source_document.to_byte_range(outcome.hover.range, encoding)
                    _check_deadline(deadline)
                except (TypeError, ValueError):
                    partial_hover = True
        graph_provenance = (
            Provenance(
                source="graph",
                provider="evidence-graph",
                version="structural",
                observation="graph_candidate",
            ),
        )
        graph_locations, partial_graph = _normalize_structural_locations(
            self._repository,
            raw_graph_locations,
            revision_entries=revision_entries,
            documents=documents,
            provenance=graph_provenance,
            deadline=deadline,
        )
        if outcome.references_require_graph:
            graph_spans = {
                (location.path, location.range.byte_start, location.range.byte_end)
                for location in graph_locations
            }
            normalized_locations = tuple(
                location
                for location in normalized_locations
                if (location.path, location.range.byte_start, location.range.byte_end)
                in graph_spans
            )
        warnings: tuple[str, ...] = ()
        if provider_failure is not None:
            warnings = (*warnings, "provider setup is not ready")
        elif outcome.failure is NavigationStatus.TIMEOUT:
            warnings = (*warnings, "provider request timed out")
        elif outcome.failure is NavigationStatus.ERROR:
            warnings = (*warnings, "provider request failed")
        elif outcome.failure is NavigationStatus.NOT_READY:
            warnings = (*warnings, "provider is not ready")
        elif outcome.coverage == "unsupported":
            warnings = (*warnings, "provider capability is unsupported")
        elif outcome.coverage == "not_ready":
            warnings = (*warnings, "provider is not ready")
        elif outcome.partial:
            warnings = (*warnings, "provider reported partial results")
        if partial_locations:
            warnings = (*warnings, "provider locations partially filtered")
        if partial_diagnostics:
            warnings = (*warnings, "provider diagnostics partially filtered")
        if partial_hover:
            warnings = (*warnings, "provider hover range was filtered")
        if structural_failed:
            warnings = (*warnings, "structural fallback failed")
        if structural_input_truncated:
            warnings = (*warnings, "structural callback input bound reached")
        if partial_graph:
            warnings = (*warnings, "structural candidates partially filtered")
        merged_locations, result_truncated = _merge_locations(
            normalized_locations,
            graph_locations,
            deadline=deadline,
        )
        _check_deadline(deadline)
        diagnostic_fact_count = sum(
            1 + len(diagnostic.related) for diagnostic in normalized_diagnostics
        )
        remaining_locations = max(
            0,
            _MAX_NAVIGATION_FACTS - diagnostic_fact_count,
        )
        if len(merged_locations) > remaining_locations:
            merged_locations = merged_locations[:remaining_locations]
            result_truncated = True
        graph_only = any(
            location.resolution is ResolutionLabel.GRAPH_CANDIDATE
            for location in merged_locations
        )
        if graph_only:
            warnings = (*warnings, "structural fallback appended")
        if result_truncated:
            warnings = (*warnings, "result fact limit reached")
        if any(
            location.resolution is ResolutionLabel.LSP_AND_GRAPH
            for location in merged_locations
        ):
            resolution_label = ResolutionLabel.LSP_AND_GRAPH
        elif any(
            location.resolution is ResolutionLabel.LSP_CONFIRMED
            for location in merged_locations
        ):
            resolution_label = ResolutionLabel.LSP_CONFIRMED
        elif merged_locations:
            resolution_label = ResolutionLabel.GRAPH_CANDIDATE
        else:
            resolution_label = ResolutionLabel.UNRESOLVED
        used_provenance: list[Provenance] = [
            item for location in merged_locations for item in location.provenance
        ]
        used_provenance.extend(
            item
            for diagnostic in normalized_diagnostics
            for item in diagnostic.provenance
        )
        if outcome.failure is None and outcome.coverage == "provider_reported":
            used_provenance.extend(lsp_provenance)
        provenance = _union_provenance(tuple(used_provenance))
        _check_deadline(deadline)
        try:
            _check_deadline(deadline)
            after_revision = _compute_post_revision(
                self._repository,
                before_revision,
                deadline=deadline,
            )
            _check_deadline(deadline)
        except TimeoutError:
            return _empty_result(
                request,
                NavigationStatus.TIMEOUT,
                revision_before=revision_before,
                revision_after=revision_before,
                provider=provider,
                provider_version=provider_version,
                readiness=self._session.readiness,
                warnings=("post-request revision timed out",),
            )
        except (OSError, ValueError, RuntimeError):
            return _empty_result(
                request,
                NavigationStatus.ERROR,
                revision_before=revision_before,
                revision_after=revision_before,
                provider=provider,
                provider_version=provider_version,
                readiness=self._session.readiness,
                warnings=("post-request revision failed",),
            )
        revision_after = after_revision.revision_sha256
        if revision_after != revision_before:
            return _empty_result(
                request,
                NavigationStatus.STALE,
                revision_before=revision_before,
                revision_after=revision_after,
                provider=provider,
                provider_version=provider_version,
                readiness=self._session.readiness,
                warnings=("workspace changed across the request",),
            )
        if request_validation_warning is not None:
            return _empty_result(
                request,
                NavigationStatus.ERROR,
                revision_before=revision_before,
                revision_after=revision_after,
                provider=provider,
                provider_version=provider_version,
                readiness=self._session.readiness,
                warnings=(request_validation_warning,),
            )
        useful_structural = any(
            any(item.provider == "evidence-graph" for item in location.provenance)
            for location in merged_locations
        )
        if outcome.failure is not None:
            status = NavigationStatus.PARTIAL if useful_structural else outcome.failure
        elif outcome.coverage == "unsupported":
            status = (
                NavigationStatus.PARTIAL
                if useful_structural
                else NavigationStatus.UNSUPPORTED
            )
        elif outcome.coverage == "not_ready":
            status = (
                NavigationStatus.PARTIAL
                if useful_structural
                else NavigationStatus.NOT_READY
            )
        else:
            status = NavigationStatus.OK if not warnings else NavigationStatus.PARTIAL
        if status is NavigationStatus.UNSUPPORTED:
            resolution_label = ResolutionLabel.UNSUPPORTED
        result = NavigationResult(
            status=status,
            requested_capability=request.capability,
            effective_capability=outcome.effective_capability,
            provider=provider,
            provider_version=provider_version,
            repository_id=self._repository.repository_id,
            checkout_id=self._repository.checkout_id,
            workspace_revision_before=revision_before,
            workspace_revision_after=revision_after,
            document_version=outcome.document_version,
            position_encoding=(
                encoding if isinstance(encoding, PositionEncoding) else None
            ),
            readiness=self._session.readiness,
            symbol=None,
            total=(
                len(normalized_diagnostics)
                if request.capability is Capability.DIAGNOSTICS
                else len(merged_locations)
            ),
            offset=request.offset,
            limit=request.limit,
            locations=merged_locations,
            diagnostics=normalized_diagnostics,
            hover=outcome.hover.contents if outcome.hover is not None else None,
            resolution=resolution_label,
            provenance=provenance,
            warnings=warnings,
        )
        _check_deadline(deadline)
        if result.status in {NavigationStatus.OK, NavigationStatus.PARTIAL}:
            self._publish_source_documents(revision_entries, documents)
        return result

    def _provider_request(
        self,
        request: NavigationRequest,
        anchor: SourceAnchor,
        document: OpenDocument,
        *,
        deadline: float,
    ) -> _ProviderOutcome:
        def location_outcome(result, capability: Capability) -> _ProviderOutcome:
            _check_deadline(deadline)
            return _ProviderOutcome(
                result.locations,
                (),
                None,
                result.coverage,
                result.partial,
                capability if result.coverage != "unsupported" else None,
                document.version,
            )

        capability = request.capability
        if capability is Capability.DEFINITIONS:
            _check_deadline(deadline)
            result = self._session.definition(anchor, deadline=deadline)
            _check_deadline(deadline)
            return location_outcome(result, capability)
        if capability is Capability.REFERENCES:
            _check_deadline(deadline)
            result = self._session.references(anchor, deadline=deadline)
            _check_deadline(deadline)
            return location_outcome(result, capability)
        if capability is Capability.IMPLEMENTATIONS:
            _check_deadline(deadline)
            result = self._session.implementations(anchor, deadline=deadline)
            _check_deadline(deadline)
            return location_outcome(result, capability)
        if capability is Capability.TYPE_DEFINITIONS:
            _check_deadline(deadline)
            result = self._session.type_definition(anchor, deadline=deadline)
            _check_deadline(deadline)
            return location_outcome(result, capability)
        if capability is Capability.TYPES:
            type_failure: NavigationStatus | None = None
            try:
                _check_deadline(deadline)
                type_result = self._session.type_definition(anchor, deadline=deadline)
                _check_deadline(deadline)
            except TimeoutError:
                type_result = ProviderLocations((), "not_ready", True)
                type_failure = NavigationStatus.TIMEOUT
            except (OSError, ValueError, RuntimeError):
                type_result = ProviderLocations((), "not_ready", True)
                type_failure = NavigationStatus.ERROR
            hover_failure: NavigationStatus | None = None
            try:
                _check_deadline(deadline)
                hover_result = self._session.hover(anchor, deadline=deadline)
                _check_deadline(deadline)
            except TimeoutError:
                hover_result = ProviderHover(None, None, True)
                hover_failure = NavigationStatus.TIMEOUT
            except (OSError, ValueError, RuntimeError):
                hover_result = ProviderHover(None, None, True)
                hover_failure = NavigationStatus.ERROR
            type_available = (
                type_failure is None and type_result.coverage == "provider_reported"
            )
            hover_available = hover_failure is None and (
                hover_result.contents is not None
                or hover_result.range is not None
                or not hover_result.partial
            )
            if type_available or hover_available:
                coverage = "provider_reported"
                failure = None
                effective_capability = capability
            else:
                coverage = type_result.coverage
                failure = (
                    NavigationStatus.TIMEOUT
                    if NavigationStatus.TIMEOUT in {type_failure, hover_failure}
                    else type_failure or hover_failure
                )
                effective_capability = (
                    capability if coverage != "unsupported" else None
                )
            return _ProviderOutcome(
                type_result.locations,
                (),
                hover_result,
                coverage,
                (
                    type_result.partial
                    or hover_result.partial
                    or not type_available
                    or not hover_available
                ),
                effective_capability,
                document.version,
                failure,
            )
        if capability is Capability.DIAGNOSTICS:
            _check_deadline(deadline)
            result: ProviderDiagnostics = self._session.diagnostics(
                request.path, deadline=deadline
            )
            _check_deadline(deadline)
            coverage = (
                "provider_reported"
                if result.document_version is not None or result.diagnostics
                else "not_ready"
            )
            return _ProviderOutcome(
                (),
                result.diagnostics,
                None,
                coverage,
                result.partial,
                capability,
                result.document_version,
            )
        if capability is Capability.CALLS:
            _check_deadline(deadline)
            calls = (
                self._session.incoming_calls(anchor, deadline=deadline)
                if request.direction == "incoming"
                else self._session.outgoing_calls(anchor, deadline=deadline)
            )
            _check_deadline(deadline)
            if (
                calls.coverage == "unsupported"
                and self._structural_candidates is not None
            ):
                _check_deadline(deadline)
                references = self._session.references(anchor, deadline=deadline)
                _check_deadline(deadline)
                return _ProviderOutcome(
                    references.locations,
                    (),
                    None,
                    references.coverage,
                    True,
                    (
                        Capability.REFERENCES
                        if references.coverage != "unsupported"
                        else None
                    ),
                    document.version,
                    references_require_graph=True,
                )
            return location_outcome(calls, capability)
        return _ProviderOutcome(
            (),
            (),
            None,
            "unsupported",
            True,
            None,
            document.version,
        )

    def resolve_symbol(
        self,
        symbol: str,
        *,
        repository: RepositoryScope,
        deadline: float | None = None,
    ) -> NavigationResult:
        if not isinstance(symbol, str) or not symbol:
            raise ValueError("symbol must be a non-empty string")
        if not isinstance(repository, RepositoryScope):
            raise TypeError("repository must be a RepositoryScope")
        if repository.checkout_id != self._repository.checkout_id:
            raise ValueError("repository must match this navigation repository")
        if deadline is not None:
            if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
                raise TypeError("deadline must be a monotonic timestamp or None")
            if not math.isfinite(deadline):
                raise ValueError("deadline must be finite")
            if time.monotonic() >= deadline:
                return _structural_result(
                    repository,
                    status=NavigationStatus.TIMEOUT,
                    capability=Capability.DEFINITIONS,
                    revision_before="",
                    revision_after="",
                    readiness=self._session.readiness,
                    symbol=symbol,
                    warnings=("deadline expired before symbol resolution",),
                )
        graph_provenance = (
            Provenance(
                source="graph",
                provider="evidence-graph",
                version="structural",
                observation="name_resolution",
            ),
        )
        prior_revision = ""
        for attempt in range(2):
            try:
                _check_deadline(deadline)
                before = _compute_revision(self._repository, deadline=deadline)
                _check_deadline(deadline)
            except TimeoutError:
                return _structural_result(
                    repository,
                    status=NavigationStatus.TIMEOUT,
                    capability=Capability.DEFINITIONS,
                    revision_before=prior_revision,
                    revision_after=prior_revision,
                    readiness=self._session.readiness,
                    symbol=symbol,
                    warnings=("symbol revision computation timed out",),
                )
            except (OSError, ValueError, RuntimeError):
                return _structural_result(
                    repository,
                    status=NavigationStatus.ERROR,
                    capability=Capability.DEFINITIONS,
                    revision_before=prior_revision,
                    revision_after=prior_revision,
                    readiness=self._session.readiness,
                    symbol=symbol,
                    warnings=("symbol revision computation failed",),
                )
            revision_before = before.revision_sha256
            if not revision_before:
                return _structural_result(
                    repository,
                    status=NavigationStatus.ERROR,
                    capability=Capability.DEFINITIONS,
                    revision_before="",
                    revision_after="",
                    readiness=self._session.readiness,
                    symbol=symbol,
                    warnings=("symbol revision is unavailable",),
                )
            try:
                revision_entries = _index_revision_entries(before, deadline=deadline)
            except TimeoutError:
                return _structural_result(
                    repository,
                    status=NavigationStatus.TIMEOUT,
                    capability=Capability.DEFINITIONS,
                    revision_before=revision_before,
                    revision_after=revision_before,
                    readiness=self._session.readiness,
                    symbol=symbol,
                    warnings=("symbol revision indexing timed out",),
                )
            raw_candidates: tuple[NavigationLocation, ...] = ()
            resolver_input_truncated = False
            resolver_failure: NavigationStatus | None = None
            if self._symbol_resolver is not None:
                try:
                    _check_deadline(deadline)
                    resolver_values = self._symbol_resolver(
                        symbol, repository, deadline
                    )
                    _check_deadline(deadline)
                    raw_candidates, resolver_input_truncated = (
                        _bounded_callback_values(
                            resolver_values,
                            deadline=deadline,
                        )
                    )
                    _check_deadline(deadline)
                except TimeoutError:
                    resolver_failure = NavigationStatus.TIMEOUT
                except NavigationInterruption:
                    raise
                except Exception:
                    resolver_failure = NavigationStatus.ERROR
            documents = self._seed_source_documents(revision_entries)
            try:
                candidates, filtered = _normalize_structural_locations(
                    repository,
                    raw_candidates,
                    revision_entries=revision_entries,
                    documents=documents,
                    provenance=graph_provenance,
                    deadline=deadline,
                )
            except TimeoutError:
                return _structural_result(
                    repository,
                    status=NavigationStatus.TIMEOUT,
                    capability=Capability.DEFINITIONS,
                    revision_before=revision_before,
                    revision_after=revision_before,
                    readiness=self._session.readiness,
                    symbol=symbol,
                    warnings=("symbol normalization timed out",),
                )
            except _RevisionMismatch:
                if attempt == 0:
                    prior_revision = revision_before
                    continue
                return _structural_result(
                    repository,
                    status=NavigationStatus.STALE,
                    capability=Capability.DEFINITIONS,
                    revision_before=revision_before,
                    revision_after=revision_before,
                    readiness=self._session.readiness,
                    symbol=symbol,
                    warnings=("symbol target changed during resolution",),
                )
            candidates = _dedupe_locations(candidates, deadline=deadline)
            _check_deadline(deadline)
            truncated = len(candidates) > _MAX_NAVIGATION_FACTS
            candidates = candidates[:_MAX_NAVIGATION_FACTS]
            if len(candidates) == 1 and not resolver_input_truncated:
                candidates = (
                    replace(
                        candidates[0],
                        resolution=ResolutionLabel.GRAPH_CONFIRMED,
                    ),
                )
                resolution = ResolutionLabel.GRAPH_CONFIRMED
            elif candidates:
                candidates = tuple(
                    replace(location, resolution=ResolutionLabel.AMBIGUOUS)
                    for location in candidates
                )
                resolution = ResolutionLabel.AMBIGUOUS
            else:
                resolution = ResolutionLabel.UNRESOLVED
            _check_deadline(deadline)
            try:
                _check_deadline(deadline)
                after = _compute_post_revision(
                    self._repository,
                    before,
                    deadline=deadline,
                )
                _check_deadline(deadline)
            except TimeoutError:
                return _structural_result(
                    repository,
                    status=NavigationStatus.TIMEOUT,
                    capability=Capability.DEFINITIONS,
                    revision_before=revision_before,
                    revision_after=revision_before,
                    readiness=self._session.readiness,
                    symbol=symbol,
                    warnings=("post-resolution revision timed out",),
                )
            except (OSError, ValueError, RuntimeError):
                return _structural_result(
                    repository,
                    status=NavigationStatus.ERROR,
                    capability=Capability.DEFINITIONS,
                    revision_before=revision_before,
                    revision_after=revision_before,
                    readiness=self._session.readiness,
                    symbol=symbol,
                    warnings=("post-resolution revision failed",),
                )
            revision_after = after.revision_sha256
            if revision_after != revision_before:
                if attempt == 0:
                    prior_revision = revision_before
                    continue
                return _structural_result(
                    repository,
                    status=NavigationStatus.STALE,
                    capability=Capability.DEFINITIONS,
                    revision_before=revision_before,
                    revision_after=revision_after,
                    readiness=self._session.readiness,
                    symbol=symbol,
                    warnings=("workspace changed across symbol resolution",),
                )
            warnings: tuple[str, ...] = ()
            if resolver_failure is NavigationStatus.TIMEOUT:
                warnings = (*warnings, "symbol resolver timed out")
            elif resolver_failure is NavigationStatus.ERROR:
                warnings = (*warnings, "symbol resolver failed")
            if filtered:
                warnings = (*warnings, "symbol candidates partially filtered")
            if resolver_input_truncated:
                warnings = (*warnings, "symbol callback input bound reached")
            if truncated:
                warnings = (*warnings, "symbol candidate limit reached")
            if len(candidates) > 1:
                warnings = (*warnings, "multiple declarations require disambiguation")
            elif not candidates and resolver_failure is None:
                warnings = (*warnings, "no structural candidates")
            if resolver_failure is not None and not candidates:
                status = resolver_failure
            elif len(candidates) == 1 and not warnings:
                status = NavigationStatus.OK
            else:
                status = NavigationStatus.PARTIAL
            provenance = _union_provenance(
                tuple(item for value in candidates for item in value.provenance)
            )
            _check_deadline(deadline)
            result = _structural_result(
                repository,
                status=status,
                capability=Capability.DEFINITIONS,
                revision_before=revision_before,
                revision_after=revision_after,
                readiness=self._session.readiness,
                symbol=symbol,
                locations=candidates,
                resolution=resolution,
                provenance=provenance,
                warnings=warnings,
            )
            try:
                _check_deadline(deadline)
            except TimeoutError:
                return _structural_result(
                    repository,
                    status=NavigationStatus.TIMEOUT,
                    capability=Capability.DEFINITIONS,
                    revision_before=revision_before,
                    revision_after=revision_after,
                    readiness=self._session.readiness,
                    symbol=symbol,
                    warnings=("symbol result construction timed out",),
                )
            if result.status in {NavigationStatus.OK, NavigationStatus.PARTIAL}:
                self._publish_source_documents(revision_entries, documents)
            return result
        raise AssertionError("symbol resolution attempt loop must return")

    def verify_edge(
        self,
        source: SourceAnchor,
        target: SourceAnchor,
        *,
        repository: RepositoryScope,
        deadline: float,
    ) -> NavigationResult:
        if not isinstance(source, SourceAnchor):
            raise TypeError("source must be a SourceAnchor")
        if not isinstance(target, SourceAnchor):
            raise TypeError("target must be a SourceAnchor")
        if not isinstance(repository, RepositoryScope):
            raise TypeError("repository must be a RepositoryScope")
        if repository.checkout_id != self._repository.checkout_id:
            raise ValueError("repository must match this navigation repository")
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
            raise TypeError("deadline must be a monotonic timestamp")
        if not math.isfinite(deadline):
            raise ValueError("deadline must be finite")
        if time.monotonic() >= deadline:
            return _structural_result(
                repository,
                status=NavigationStatus.TIMEOUT,
                capability=Capability.CALLS,
                revision_before="",
                revision_after="",
                readiness=self._session.readiness,
                symbol=None,
                warnings=("deadline expired before edge verification",),
            )
        graph_provenance = (
            Provenance(
                source="graph",
                provider="evidence-graph",
                version="structural",
                observation="edge_verification",
            ),
        )
        prior_revision = ""
        for attempt in range(2):
            try:
                _check_deadline(deadline)
                before = _compute_revision(self._repository, deadline=deadline)
                _check_deadline(deadline)
            except TimeoutError:
                return _structural_result(
                    repository,
                    status=NavigationStatus.TIMEOUT,
                    capability=Capability.CALLS,
                    revision_before=prior_revision,
                    revision_after=prior_revision,
                    readiness=self._session.readiness,
                    symbol=None,
                    warnings=("edge revision computation timed out",),
                )
            except (OSError, ValueError, RuntimeError):
                return _structural_result(
                    repository,
                    status=NavigationStatus.ERROR,
                    capability=Capability.CALLS,
                    revision_before=prior_revision,
                    revision_after=prior_revision,
                    readiness=self._session.readiness,
                    symbol=None,
                    warnings=("edge revision computation failed",),
                )
            revision_before = before.revision_sha256
            if not revision_before:
                return _structural_result(
                    repository,
                    status=NavigationStatus.ERROR,
                    capability=Capability.CALLS,
                    revision_before="",
                    revision_after="",
                    readiness=self._session.readiness,
                    symbol=None,
                    warnings=("edge revision is unavailable",),
                )
            try:
                revision_entries = _index_revision_entries(before, deadline=deadline)
            except TimeoutError:
                return _structural_result(
                    repository,
                    status=NavigationStatus.TIMEOUT,
                    capability=Capability.CALLS,
                    revision_before=revision_before,
                    revision_after=revision_before,
                    readiness=self._session.readiness,
                    symbol=None,
                    warnings=("edge revision indexing timed out",),
                )
            documents = self._seed_source_documents(revision_entries)
            try:
                source_valid = _validate_revision_anchor(
                    repository,
                    source,
                    revision_entries,
                    documents,
                    deadline=deadline,
                )
                target_valid = _validate_revision_anchor(
                    repository,
                    target,
                    revision_entries,
                    documents,
                    deadline=deadline,
                )
                anchors_valid = source_valid and target_valid
            except TimeoutError:
                return _structural_result(
                    repository,
                    status=NavigationStatus.TIMEOUT,
                    capability=Capability.CALLS,
                    revision_before=revision_before,
                    revision_after=revision_before,
                    readiness=self._session.readiness,
                    symbol=None,
                    warnings=("edge anchor validation timed out",),
                )
            except _RevisionMismatch:
                if attempt == 0:
                    prior_revision = revision_before
                    continue
                return _structural_result(
                    repository,
                    status=NavigationStatus.STALE,
                    capability=Capability.CALLS,
                    revision_before=revision_before,
                    revision_after=revision_before,
                    readiness=self._session.readiness,
                    symbol=None,
                    warnings=("edge target changed during verification",),
                )
            verified: bool | None = None
            verifier_failure: NavigationStatus | None = None
            if anchors_valid and self._edge_verifier is not None:
                try:
                    _check_deadline(deadline)
                    value = self._edge_verifier(source, target, repository, deadline)
                    _check_deadline(deadline)
                    if not isinstance(value, bool):
                        raise TypeError("edge verifier result must be boolean")
                    verified = value
                except TimeoutError:
                    verifier_failure = NavigationStatus.TIMEOUT
                except NavigationInterruption:
                    raise
                except Exception:
                    verifier_failure = NavigationStatus.ERROR
            try:
                _check_deadline(deadline)
                after = _compute_post_revision(
                    self._repository,
                    before,
                    deadline=deadline,
                )
                _check_deadline(deadline)
            except TimeoutError:
                return _structural_result(
                    repository,
                    status=NavigationStatus.TIMEOUT,
                    capability=Capability.CALLS,
                    revision_before=revision_before,
                    revision_after=revision_before,
                    readiness=self._session.readiness,
                    symbol=None,
                    warnings=("post-edge revision timed out",),
                )
            except (OSError, ValueError, RuntimeError):
                return _structural_result(
                    repository,
                    status=NavigationStatus.ERROR,
                    capability=Capability.CALLS,
                    revision_before=revision_before,
                    revision_after=revision_before,
                    readiness=self._session.readiness,
                    symbol=None,
                    warnings=("post-edge revision failed",),
                )
            revision_after = after.revision_sha256
            if revision_after != revision_before:
                if attempt == 0:
                    prior_revision = revision_before
                    continue
                return _structural_result(
                    repository,
                    status=NavigationStatus.STALE,
                    capability=Capability.CALLS,
                    revision_before=revision_before,
                    revision_after=revision_after,
                    readiness=self._session.readiness,
                    symbol=None,
                    warnings=("workspace changed across edge verification",),
                )
            if not anchors_valid:
                return _structural_result(
                    repository,
                    status=NavigationStatus.ERROR,
                    capability=Capability.CALLS,
                    revision_before=revision_before,
                    revision_after=revision_after,
                    readiness=self._session.readiness,
                    symbol=None,
                    warnings=("edge anchor validation failed",),
                )
            if verifier_failure is not None:
                return _structural_result(
                    repository,
                    status=verifier_failure,
                    capability=Capability.CALLS,
                    revision_before=revision_before,
                    revision_after=revision_after,
                    readiness=self._session.readiness,
                    symbol=None,
                    warnings=(
                        "edge verifier timed out"
                        if verifier_failure is NavigationStatus.TIMEOUT
                        else "edge verifier failed",
                    ),
                )
            if verified is not True:
                self._publish_source_documents(revision_entries, documents)
                return _structural_result(
                    repository,
                    status=NavigationStatus.PARTIAL,
                    capability=Capability.CALLS,
                    revision_before=revision_before,
                    revision_after=revision_after,
                    readiness=self._session.readiness,
                    symbol=None,
                    warnings=("no structural edge proof",),
                )
            location = NavigationLocation(
                target.path,
                PositionRange(target.byte_offset, target.byte_offset),
                target.line,
                target.utf8_character,
                None,
                None,
                ResolutionLabel.GRAPH_CONFIRMED,
                graph_provenance,
            )
            _check_deadline(deadline)
            result = _structural_result(
                repository,
                status=NavigationStatus.OK,
                capability=Capability.CALLS,
                revision_before=revision_before,
                revision_after=revision_after,
                readiness=self._session.readiness,
                symbol=None,
                locations=(location,),
                resolution=ResolutionLabel.GRAPH_CONFIRMED,
                provenance=graph_provenance,
            )
            try:
                _check_deadline(deadline)
            except TimeoutError:
                return _structural_result(
                    repository,
                    status=NavigationStatus.TIMEOUT,
                    capability=Capability.CALLS,
                    revision_before=revision_before,
                    revision_after=revision_after,
                    readiness=self._session.readiness,
                    symbol=None,
                    warnings=("edge result construction timed out",),
                )
            self._publish_source_documents(revision_entries, documents)
            return result
        raise AssertionError("edge verification attempt loop must return")

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
