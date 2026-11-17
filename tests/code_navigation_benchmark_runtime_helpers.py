"""The semantic fake runtime and navigation results the Python navigation benchmark test files share."""

from __future__ import annotations

import hashlib
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

BENCHMARK_ROOT = Path(__file__).resolve().parent.parent / "benchmark"
SCRIPTS_ROOT = Path(__file__).resolve().parent.parent / "scripts"
for path in (BENCHMARK_ROOT, SCRIPTS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from code_intelligence import Capability, PositionEncoding, PositionRange  # noqa: E402
from code_navigation import (  # noqa: E402
    NavigationLocation,
    NavigationResult,
    NavigationStatus,
    Provenance,
    ResolutionLabel,
)
from generate_python_qualification import (  # noqa: E402
    PYRIGHT_VERSION,
    GoldLocation,
    GoldQuery,
    QualificationRepository,
)
from lsp_positions import LspPosition, LspRange, path_to_file_uri  # noqa: E402
from pyright_session import LspLocation  # noqa: E402

from tests.code_navigation_benchmark_helpers import PACKAGE_SHA256  # noqa: E402
from tests.modules.python_qualification_corpus.support import (  # noqa: E402
    _line_start_bytes,
)

_PYRIGHT_PROVENANCE = (
    Provenance("lsp", "pyright", PYRIGHT_VERSION, "provider_reported"),
)


def _answer_status(symbol: str | None) -> NavigationStatus:
    if symbol is None:
        return NavigationStatus.ERROR
    return NavigationStatus.OK


def _answered_capability(request: object, symbol: str | None):
    if symbol is None:
        return None
    return request.capability


def _resolution_for(locations: tuple) -> ResolutionLabel:
    if locations:
        return ResolutionLabel.LSP_CONFIRMED
    return ResolutionLabel.UNRESOLVED


def _symbol_at(line: bytes, character: int) -> str:
    return line[character:].split(b"(", 1)[0].strip().decode("utf-8")


def _is_import_of(source_line: str, suffix: str) -> bool:
    if not source_line.startswith("from "):
        return False
    return source_line.endswith(suffix)


def _imported_module(content: bytes, symbol: str) -> str | None:
    """The module a `from X import <symbol>` line names, if any line does."""
    suffix = f" import {symbol}"
    for source_line in content.decode("utf-8").splitlines():
        if _is_import_of(source_line, suffix):
            return source_line[5 : -len(suffix)]
    return None


def _definition_in(target_content: bytes, target_path: str, symbol: str):
    """The location of `def <symbol>(`, or nothing when the file lacks it."""
    byte_start = target_content.find(f"def {symbol}(".encode())
    if byte_start < 0:
        return None
    byte_start += len(b"def ")
    prefix = target_content[:byte_start]
    return (
        GoldLocation(
            target_path,
            prefix.count(b"\n") + 1,
            byte_start - (prefix.rfind(b"\n") + 1),
            byte_start,
            byte_start + len(symbol.encode()),
            hashlib.sha256(target_content).hexdigest(),
        ),
    )


class BehavioralNavigationRuntime:
    """Small semantic fake that returns locations, never metric values."""

    def __init__(
        self,
        repository: QualificationRepository,
        scope: object,
        *,
        mismatch_query_id: str | None = None,
    ) -> None:
        self.repository = repository
        self.scope = scope
        self.mismatch_query_id = mismatch_query_id
        self.calls: list[object] = []
        self._queries = {
            (
                query.capability,
                query.path,
                query.line,
                query.character,
                query.direction,
            ): query
            for query in repository.gold_queries
        }

    @staticmethod
    def _capability_name(capability: Capability) -> str:
        return {
            Capability.DEFINITIONS: "definition",
            Capability.REFERENCES: "references",
            Capability.CALLS: "calls",
        }[capability]

    def _definition_locations(self, module: str, symbol: str):
        """No location when the target file is absent; None when it lacks the symbol."""
        target_path = f"{module.replace('.', '/')}.py"
        target = self.repository.root / target_path
        if not target.is_file():
            return ()
        return _definition_in(target.read_bytes(), target_path, symbol)

    def _dynamic_query(self, request: object) -> GoldQuery | None:
        path = self.repository.root / request.path
        if not path.is_file():
            return None
        content = path.read_bytes()
        lines = content.splitlines(keepends=True)
        symbol = _symbol_at(lines[request.line - 1], request.character)
        return self._imported_definition_query(request, content, symbol)

    def _imported_definition_query(
        self,
        request: object,
        content: bytes,
        symbol: str,
    ) -> GoldQuery | None:
        """The symbol must be imported, and its module must define it."""
        module = _imported_module(content, symbol)
        if not module:
            return None
        expected = self._definition_locations(module, symbol)
        if expected is None:
            return None
        digest = hashlib.sha256(content).hexdigest()
        query_start = _line_start_bytes(content, request.line) + request.character
        return GoldQuery(
            "dynamic",
            "definition",
            request.path,
            request.line,
            request.character,
            request.character,
            query_start,
            query_start + len(symbol.encode()),
            digest,
            symbol,
            None,
            expected,
        )

    def _query_for_request(self, request: object) -> GoldQuery | None:
        key = (
            self._capability_name(request.capability),
            request.path,
            request.line,
            request.character,
            request.direction,
        )
        return self._queries.get(key) or self._dynamic_query(request)

    def _direct_result_for_query(self, query: GoldQuery) -> object:
        locations = tuple(
            LspLocation(
                path_to_file_uri(self.repository.root / location.path),
                LspRange(
                    LspPosition(location.line - 1, location.character),
                    LspPosition(
                        location.line - 1,
                        location.character + location.byte_end - location.byte_start,
                    ),
                ),
            )
            for location in query.expected_locations
        )
        return SimpleNamespace(
            coverage="provider_reported",
            partial=False,
            locations=locations,
        )

    @property
    def position_encoding(self) -> PositionEncoding:
        return PositionEncoding.UTF8

    def query(self, request: object, *, deadline: float) -> NavigationResult:
        assert deadline > time.monotonic()
        self.calls.append(request)
        query = self._query_for_request(request)
        if query is None:
            return self._navigation_result(request, None, ())
        locations = self._navigation_locations(self._answered_locations(query))
        return self._navigation_result(request, query.symbol, locations)

    def _answered_locations(self, query: GoldQuery) -> tuple:
        """The query under mismatch is the one that answers with nothing."""
        if query.query_id == self.mismatch_query_id:
            return ()
        return query.expected_locations

    def _navigation_locations(self, expected) -> tuple:
        return tuple(
            NavigationLocation(
                location.path,
                PositionRange(location.byte_start, location.byte_end),
                location.line,
                location.character,
                None,
                None,
                ResolutionLabel.LSP_CONFIRMED,
                _PYRIGHT_PROVENANCE,
            )
            for location in expected
        )

    def _navigation_result(
        self,
        request: object,
        symbol: str | None,
        locations: tuple,
    ) -> NavigationResult:
        """An answered query, or — with no symbol — the error shape."""
        return NavigationResult(
            status=_answer_status(symbol),
            requested_capability=request.capability,
            effective_capability=_answered_capability(request, symbol),
            provider="pyright",
            provider_version=PYRIGHT_VERSION,
            repository_id=self.scope.repository_id,
            checkout_id=self.scope.checkout_id,
            workspace_revision_before="a" * 64,
            workspace_revision_after="a" * 64,
            document_version=1,
            position_encoding=PositionEncoding.UTF8,
            readiness="query_ready",
            symbol=symbol,
            total=len(locations),
            offset=request.offset,
            limit=request.limit,
            locations=locations,
            diagnostics=(),
            hover=None,
            resolution=_resolution_for(locations),
            provenance=_PYRIGHT_PROVENANCE,
            warnings=(),
        )

    def direct_query(self, request: object, *, deadline: float) -> object:
        assert deadline > time.monotonic()
        query = self._query_for_request(request)
        assert query is not None
        return self._direct_result_for_query(query)

    def synchronize(self, *, deadline: float) -> None:
        assert deadline > time.monotonic()

    def crash_and_recover(self, request: object, *, deadline: float) -> NavigationResult:
        return self.query(request, deadline=deadline)

    def ownership_checks(self, *, deadline: float) -> dict[str, dict[str, int | bool]]:
        assert deadline > time.monotonic()
        return {
            scenario: {"available": True, "orphan_count": 0}
            for scenario in (
                "normal_shutdown",
                "crash",
                "timeout",
                "cancellation",
            )
        }

    def close(self, *, deadline: float) -> int:
        assert deadline > time.monotonic()
        return 0


def _qualified_identity() -> SimpleNamespace:
    return SimpleNamespace(
        status="qualified",
        qualified=True,
        version=PYRIGHT_VERSION,
        package_sha256=PACKAGE_SHA256,
        node_version="v22.15.0",
        node_major=22,
        degradation_codes=(),
    )


def _empty_navigation_result(request: object) -> NavigationResult:
    return NavigationResult(
        NavigationStatus.ERROR,
        request.capability,
        None,
        "pyright",
        PYRIGHT_VERSION,
        request.repository.repository_id,
        request.repository.checkout_id,
        "a" * 64,
        "a" * 64,
        None,
        PositionEncoding.UTF8,
        "query_ready",
        None,
        0,
        request.offset,
        request.limit,
        (),
        (),
        None,
        ResolutionLabel.UNRESOLVED,
        (),
        (),
    )


def _result_with_locations(
    request: object,
    status: NavigationStatus,
    locations: tuple[NavigationLocation, ...],
) -> NavigationResult:
    provenance = (Provenance("lsp", "pyright", PYRIGHT_VERSION, "provider_reported"),)
    return replace(
        _empty_navigation_result(request),
        status=status,
        effective_capability=request.capability,
        symbol="measured",
        total=len(locations),
        locations=locations,
        resolution=(ResolutionLabel.LSP_CONFIRMED if locations else ResolutionLabel.UNRESOLVED),
        provenance=provenance,
    )
