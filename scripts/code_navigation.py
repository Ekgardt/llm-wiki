"""Normalized, freshness-proven, capability-honest code navigation facade."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from code_intelligence import Capability, PositionEncoding, PositionRange
from lsp_positions import SourceAnchor, SourceDocument
from lsp_security import normalize_provider_uri
from pyright_profile import PyrightIdentity
from pyright_session import (
    LspLocation,
    OpenDocument,
    ProviderHover,
    PyrightSession,
)
from repository_scope import RepositoryScope
from workspace_revision import (
    WorkspaceRevision,
    compute_workspace_revision,
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


@dataclass(frozen=True, slots=True)
class Provenance:
    source: str
    provider: str
    version: str
    observation: str


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


@dataclass(frozen=True, slots=True)
class NavigationDiagnostic:
    path: str
    range: PositionRange
    severity: int | None
    code: str | None
    message: str
    related: tuple[NavigationLocation, ...]
    provenance: tuple[Provenance, ...]


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
        if not isinstance(self.path, str) or not self.path:
            raise ValueError("path must be a non-empty string")
        if (
            isinstance(self.line, bool)
            or not isinstance(self.line, int)
            or self.line < 1
        ):
            raise ValueError("line must be a one-based positive integer")
        if (
            isinstance(self.character, bool)
            or not isinstance(self.character, int)
            or self.character < 0
        ):
            raise ValueError("character must be a non-negative UTF-8 byte offset")
        if (
            isinstance(self.offset, bool)
            or not isinstance(self.offset, int)
            or self.offset < 0
        ):
            raise ValueError("offset must be non-negative")
        if (
            isinstance(self.limit, bool)
            or not isinstance(self.limit, int)
            or not 1 <= self.limit <= 100
        ):
            raise ValueError("limit must be between 1 and 100")
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
        warnings=warnings,
    )


class NavigationInterruption(Exception):
    """Raised to stop a navigation attempt cleanly without partial publication."""


def _range_from_lsp(location: LspLocation, document: SourceDocument) -> PositionRange | None:
    encoding = PositionEncoding.UTF8
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
    documents: Mapping[str, SourceDocument],
) -> tuple[tuple[NavigationLocation, ...], bool]:
    normalized: list[NavigationLocation] = []
    partial = False
    for location in locations:
        source = normalize_provider_uri(repository, location.uri)
        if source is None:
            partial = True
            continue
        document = documents.get(source.uri)
        if document is None:
            try:
                document = SourceDocument.from_bytes(source.relative_path, b"")
            except (ValueError, TypeError):
                partial = True
                continue
        range_ = _range_from_lsp(location, document)
        if range_ is None:
            partial = True
            continue
        line = 1
        character = 0
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
    return tuple(normalized), partial


def _location_key(location: NavigationLocation) -> tuple[object, ...]:
    return (
        location.path,
        location.range.byte_start,
        location.range.byte_end,
        location.resolution,
        location.provenance[0].source if location.provenance else "",
    )


def _dedupe_locations(
    locations: tuple[NavigationLocation, ...],
) -> tuple[NavigationLocation, ...]:
    seen: set[tuple[object, ...]] = set()
    result: list[NavigationLocation] = []
    for location in sorted(locations, key=_location_key):
        key = _location_key(location)
        if key in seen:
            continue
        seen.add(key)
        result.append(location)
    return tuple(result)


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
                provenance=graph_provenance,
            )
        )
    return tuple(appended)


def _compute_revision(
    repository: RepositoryScope,
    *,
    deadline: float,
) -> WorkspaceRevision:
    return compute_workspace_revision(repository, deadline=deadline)


class CodeNavigation:
    """Freshness-proven navigation facade over one Pyright provider session."""

    def __init__(
        self,
        repository: RepositoryScope,
        session: PyrightSession,
        identity: PyrightIdentity,
        *,
        structural_candidates: Callable[[NavigationRequest], tuple[NavigationLocation, ...]] | None = None,
        symbol_resolver: Callable[[str, RepositoryScope], tuple[NavigationLocation, ...]] | None = None,
    ) -> None:
        if not isinstance(repository, RepositoryScope):
            raise TypeError("repository must be a RepositoryScope")
        if not isinstance(session, PyrightSession):
            raise TypeError("session must be a PyrightSession")
        if not isinstance(identity, PyrightIdentity):
            raise TypeError("identity must be a PyrightIdentity")
        self._repository = repository
        self._session = session
        self._identity = identity
        self._structural_candidates = structural_candidates
        self._symbol_resolver = symbol_resolver
        self._lock = threading.Lock()

    @property
    def repository(self) -> RepositoryScope:
        return self._repository

    @property
    def provider(self) -> str:
        return "pyright"

    @property
    def provider_version(self) -> str | None:
        return self._identity.version

    def query(
        self,
        request: NavigationRequest,
        *,
        deadline: float,
    ) -> NavigationResult:
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
            raise TypeError("deadline must be a monotonic timestamp")
        if not math.isfinite(deadline):
            raise ValueError("deadline must be finite")
        if time.monotonic() >= deadline:
            return _empty_result(
                request,
                NavigationStatus.TIMEOUT,
                revision_before="",
                revision_after="",
                warnings=["deadline expired before query"],
            )
        if not isinstance(request, NavigationRequest):
            raise TypeError("request must be a NavigationRequest")
        if request.repository.checkout_id != self._repository.checkout_id:
            raise ValueError("request must target this navigation repository")
        attempts = 0
        revision_before = ""
        while True:
            attempts += 1
            try:
                revision_before_revision = _compute_revision(
                    self._repository, deadline=deadline
                )
            except TimeoutError:
                return _empty_result(
                    request,
                    NavigationStatus.TIMEOUT,
                    revision_before=revision_before,
                    revision_after=revision_before,
                    warnings=["revision computation timed out"],
                )
            except (OSError, ValueError, RuntimeError) as error:
                return _empty_result(
                    request,
                    NavigationStatus.ERROR,
                    revision_before="",
                    revision_after="",
                    warnings=[f"revision computation failed: {error}"],
                )
            revision_before = revision_before_revision.revision_sha256
            try:
                self._session.synchronize(revision_before_revision, deadline=deadline)
            except TimeoutError:
                return _empty_result(
                    request,
                    NavigationStatus.TIMEOUT,
                    revision_before=revision_before,
                    revision_after=revision_before,
                    provider=self.provider,
                    provider_version=self.provider_version,
                    warnings=["synchronization timed out"],
                )
            except (OSError, ValueError, RuntimeError):
                return _empty_result(
                    request,
                    NavigationStatus.ERROR,
                    revision_before=revision_before,
                    revision_after=revision_before,
                    provider=self.provider,
                    provider_version=self.provider_version,
                    warnings=["synchronization failed"],
                )
            outcome = self._attempt_query(
                request,
                revision_before_revision,
                deadline=deadline,
            )
            if outcome.status is not NavigationStatus.STALE or attempts >= 2:
                return outcome
            revision_before = outcome.workspace_revision_before

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
        try:
            document = self._session.open_document(request.path, deadline=deadline)
        except TimeoutError:
            return _empty_result(
                request,
                NavigationStatus.TIMEOUT,
                revision_before=revision_before,
                revision_after=revision_before,
                provider=provider,
                provider_version=provider_version,
                warnings=["document open timed out"],
            )
        except (OSError, ValueError, RuntimeError) as error:
            return _empty_result(
                request,
                NavigationStatus.ERROR,
                revision_before=revision_before,
                revision_after=revision_before,
                provider=provider,
                provider_version=provider_version,
                warnings=[f"document open failed: {error}"],
            )
        source_document = SourceDocument.from_bytes(
            document.source.relative_path,
            document.content,
        )
        if document.source_sha256 != hashlib_sha256(document.content):
            return _empty_result(
                request,
                NavigationStatus.STALE,
                revision_before=revision_before,
                revision_after=revision_before,
                provider=provider,
                provider_version=provider_version,
                warnings=["source hash mismatch after open"],
            )
        try:
            anchor = source_document.validate_anchor(
                line=request.line,
                character=request.character,
            )
        except (ValueError, TypeError) as error:
            return _empty_result(
                request,
                NavigationStatus.ERROR,
                revision_before=revision_before,
                revision_after=revision_before,
                provider=provider,
                provider_version=provider_version,
                warnings=[f"anchor validation failed: {error}"],
            )
        try:
            after_revision = _compute_revision(self._repository, deadline=deadline)
        except TimeoutError:
            return _empty_result(
                request,
                NavigationStatus.TIMEOUT,
                revision_before=revision_before,
                revision_after=revision_before,
                provider=provider,
                provider_version=provider_version,
                warnings=["post-request revision timed out"],
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
                warnings=["workspace changed across the request"],
            )
        try:
            locations, hover, diagnostics = self._provider_request(
                request, anchor, document, deadline=deadline
            )
        except TimeoutError:
            return _empty_result(
                request,
                NavigationStatus.TIMEOUT,
                revision_before=revision_before,
                revision_after=revision_after,
                provider=provider,
                provider_version=provider_version,
                warnings=["provider request timed out"],
            )
        except (OSError, ValueError, RuntimeError) as error:
            return _empty_result(
                request,
                NavigationStatus.ERROR,
                revision_before=revision_before,
                revision_after=revision_after,
                provider=provider,
                provider_version=provider_version,
                warnings=[f"provider request failed: {error}"],
            )
        documents = {document.source.uri: source_document}
        provenance = (
            Provenance(
                source="lsp",
                provider=provider,
                version=provider_version or "",
                observation="provider_reported",
            ),
        )
        normalized_locations, partial_locations = _normalize_locations(
            self._repository,
            locations,
            resolution=ResolutionLabel.LSP_CONFIRMED,
            provenance=provenance,
            documents=documents,
        )
        warnings: tuple[str, ...] = ()
        if partial_locations:
            warnings = (*warnings, "provider locations partially filtered")
        merged_locations = normalized_locations
        resolution_label = ResolutionLabel.LSP_CONFIRMED if normalized_locations else ResolutionLabel.UNRESOLVED
        if self._structural_candidates is not None:
            try:
                graph_locations = tuple(self._structural_candidates(request))
            except BaseException:
                graph_locations = ()
            if graph_locations:
                graph_provenance = (
                    Provenance(
                        source="graph",
                        provider="evidence-graph",
                        version="structural",
                        observation="graph_candidate",
                    ),
                )
                graph_only = _graph_only_candidates(
                    graph_locations, normalized_locations, graph_provenance
                )
                if graph_only:
                    merged_locations = (*normalized_locations, *graph_only)
                    resolution_label = (
                        ResolutionLabel.LSP_AND_GRAPH
                        if normalized_locations
                        else ResolutionLabel.GRAPH_CANDIDATE
                    )
                    warnings = (*warnings, "structural fallback appended")
        merged_locations = _dedupe_locations(merged_locations)
        status = NavigationStatus.OK if not warnings else NavigationStatus.PARTIAL
        return NavigationResult(
            status=status,
            requested_capability=request.capability,
            effective_capability=request.capability,
            provider=provider,
            provider_version=provider_version,
            repository_id=self._repository.repository_id,
            checkout_id=self._repository.checkout_id,
            workspace_revision_before=revision_before,
            workspace_revision_after=revision_after,
            document_version=document.version,
            position_encoding=self._session.position_encoding,
            readiness=self._session.readiness,
            symbol=None,
            total=len(merged_locations),
            offset=request.offset,
            limit=request.limit,
            locations=merged_locations,
            diagnostics=(),
            hover=hover,
            resolution=resolution_label,
            provenance=provenance,
            warnings=warnings,
        )

    def _provider_request(
        self,
        request: NavigationRequest,
        anchor: SourceAnchor,
        document: OpenDocument,
        *,
        deadline: float,
    ) -> tuple[tuple[LspLocation, ...], str | None, tuple[NavigationDiagnostic, ...]]:
        capability = request.capability
        if capability is Capability.DEFINITIONS:
            result = self._session.definition(anchor, deadline=deadline)
            return result.locations, None, ()
        if capability is Capability.REFERENCES:
            result = self._session.references(anchor, deadline=deadline)
            return result.locations, None, ()
        if capability is Capability.IMPLEMENTATIONS:
            result = self._session.implementations(anchor, deadline=deadline)
            return result.locations, None, ()
        if capability is Capability.TYPE_DEFINITIONS:
            result = self._session.type_definition(anchor, deadline=deadline)
            return result.locations, None, ()
        if capability is Capability.TYPES:
            type_result = self._session.type_definition(anchor, deadline=deadline)
            try:
                hover_result = self._session.hover(anchor, deadline=deadline)
            except (OSError, ValueError, RuntimeError, TimeoutError):
                hover_result = ProviderHover(None, None, True)
            return type_result.locations, hover_result.contents, ()
        if capability is Capability.DIAGNOSTICS:
            self._session.diagnostics(request.path, deadline=deadline)
            return (), None, ()
        if capability is Capability.CALLS:
            calls = (
                self._session.incoming_calls(anchor, deadline=deadline)
                if request.direction == "incoming"
                else self._session.outgoing_calls(anchor, deadline=deadline)
            )
            return calls.locations, None, ()
        return (), None, ()

    def resolve_symbol(
        self,
        symbol: str,
        *,
        repository: RepositoryScope,
        deadline: float | None = None,
    ) -> NavigationResult:
        if not isinstance(symbol, str) or not symbol:
            raise ValueError("symbol must be a non-empty string")
        if repository.checkout_id != self._repository.checkout_id:
            raise ValueError("repository must match this navigation repository")
        revision = ""
        try:
            revision_revision = compute_workspace_revision(
                self._repository,
                deadline=deadline,
            )
            revision = revision_revision.revision_sha256
        except (OSError, ValueError, RuntimeError, TimeoutError):
            pass
        candidates: tuple[NavigationLocation, ...] = ()
        if self._symbol_resolver is not None:
            try:
                candidates = tuple(self._symbol_resolver(symbol, repository))
            except BaseException:
                candidates = ()
        provenance = (
            Provenance(
                source="graph",
                provider="evidence-graph",
                version="structural",
                observation="name_resolution",
            ),
        )
        if len(candidates) > 1:
            return NavigationResult(
                status=NavigationStatus.PARTIAL,
                requested_capability=Capability.DEFINITIONS,
                effective_capability=Capability.DEFINITIONS,
                provider=None,
                provider_version=None,
                repository_id=self._repository.repository_id,
                checkout_id=self._repository.checkout_id,
                workspace_revision_before=revision,
                workspace_revision_after=revision,
                document_version=None,
                position_encoding=None,
                readiness="not_ready",
                symbol=symbol,
                total=len(candidates),
                offset=0,
                limit=100,
                locations=candidates,
                diagnostics=(),
                hover=None,
                resolution=ResolutionLabel.AMBIGUOUS,
                provenance=provenance,
                warnings=("multiple declarations require disambiguation",),
            )
        if len(candidates) == 1:
            return NavigationResult(
                status=NavigationStatus.OK,
                requested_capability=Capability.DEFINITIONS,
                effective_capability=Capability.DEFINITIONS,
                provider=None,
                provider_version=None,
                repository_id=self._repository.repository_id,
                checkout_id=self._repository.checkout_id,
                workspace_revision_before=revision,
                workspace_revision_after=revision,
                document_version=None,
                position_encoding=None,
                readiness="not_ready",
                symbol=symbol,
                total=1,
                offset=0,
                limit=100,
                locations=candidates,
                diagnostics=(),
                hover=None,
                resolution=ResolutionLabel.GRAPH_CONFIRMED,
                provenance=provenance,
                warnings=(),
            )
        return NavigationResult(
            status=NavigationStatus.PARTIAL,
            requested_capability=Capability.DEFINITIONS,
            effective_capability=None,
            provider=None,
            provider_version=None,
            repository_id=self._repository.repository_id,
            checkout_id=self._repository.checkout_id,
            workspace_revision_before=revision,
            workspace_revision_after=revision,
            document_version=None,
            position_encoding=None,
            readiness="not_ready",
            symbol=symbol,
            total=0,
            offset=0,
            limit=100,
            locations=(),
            diagnostics=(),
            hover=None,
            resolution=ResolutionLabel.UNRESOLVED,
            provenance=(),
            warnings=("no structural candidates",),
        )

    def verify_edge(
        self,
        source: SourceAnchor,
        target: SourceAnchor,
        *,
        repository: RepositoryScope,
        deadline: float,
    ) -> NavigationResult:
        if repository.checkout_id != self._repository.checkout_id:
            raise ValueError("repository must match this navigation repository")
        revision = ""
        try:
            revision_revision = compute_workspace_revision(
                self._repository,
                deadline=deadline,
            )
            revision = revision_revision.revision_sha256
        except (OSError, ValueError, RuntimeError, TimeoutError):
            pass
        provenance = (
            Provenance(
                source="graph",
                provider="evidence-graph",
                version="structural",
                observation="edge_verification",
            ),
        )
        return NavigationResult(
            status=NavigationStatus.PARTIAL,
            requested_capability=Capability.CALLS,
            effective_capability=Capability.CALLS,
            provider=None,
            provider_version=None,
            repository_id=self._repository.repository_id,
            checkout_id=self._repository.checkout_id,
            workspace_revision_before=revision,
            workspace_revision_after=revision,
            document_version=None,
            position_encoding=None,
            readiness="not_ready",
            symbol=None,
            total=0,
            offset=0,
            limit=100,
            locations=(),
            diagnostics=(),
            hover=None,
            resolution=ResolutionLabel.UNRESOLVED,
            provenance=provenance,
            warnings=("structural edge verification not wired",),
        )

    def close(self, *, deadline: float) -> None:
        self._session.close(deadline=deadline)


def hashlib_sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()
