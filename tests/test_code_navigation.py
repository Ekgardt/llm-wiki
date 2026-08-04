"""Normalized code navigation contract and facade tests."""

from __future__ import annotations

import hashlib
import time
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import code_navigation
import pytest
from code_intelligence import (
    Capability,
    DiagnosticSeverity,
    PositionEncoding,
    PositionRange,
)
from code_navigation import (
    CodeNavigation,
    NavigationDiagnostic,
    NavigationLocation,
    NavigationRequest,
    NavigationResult,
    NavigationStatus,
    Provenance,
    ResolutionLabel,
)
from lsp_positions import LspPosition, LspRange, SourceAnchor, SourceDocument
from lsp_security import resolve_repository_source
from pyright_session import (
    LspDiagnostic,
    LspLocation,
    OpenDocument,
    ProviderCalls,
    ProviderDiagnostics,
    ProviderHover,
    ProviderLocations,
    PyrightSession,
)
from repository_scope import RepositoryScope, resolve_repository_scope
from workspace_revision import WorkspaceRevision, compute_workspace_revision

from tests.code_kernel_helpers import (
    SemanticPyrightFixture,
    create_semantic_pyright_fixture,
)


def test_navigation_status_has_exactly_seven_values() -> None:
    assert {member.value for member in NavigationStatus} == {
        "ok",
        "partial",
        "unsupported",
        "not_ready",
        "stale",
        "timeout",
        "error",
    }
    assert len(list(NavigationStatus)) == 7


def test_resolution_label_has_exactly_eight_values() -> None:
    assert {member.value for member in ResolutionLabel} == {
        "lsp_confirmed",
        "graph_confirmed",
        "lsp_and_graph",
        "lsp_only",
        "graph_candidate",
        "ambiguous",
        "unresolved",
        "unsupported",
    }
    assert len(list(ResolutionLabel)) == 8


def _scope(repository: Path) -> RepositoryScope:
    return resolve_repository_scope(repository)


def test_navigation_request_validates_required_fields(
    repository: Path,
) -> None:
    scope = _scope(repository)
    request = NavigationRequest(scope, Capability.DEFINITIONS, "pkg/api.py", 10, 24)
    assert request.offset == 0
    assert request.limit == 10
    assert request.direction is None


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"line": 0}, ValueError),
        ({"line": -1}, ValueError),
        ({"character": -1}, ValueError),
        ({"offset": -1}, ValueError),
        ({"limit": 0}, ValueError),
        ({"limit": 101}, ValueError),
        ({"direction": "sideways"}, ValueError),
    ],
)
def test_navigation_request_rejects_invalid_values(
    repository: Path,
    overrides: dict,
    error: type[Exception],
) -> None:
    scope = _scope(repository)
    base = {
        "repository": scope,
        "capability": Capability.DEFINITIONS,
        "path": "pkg/api.py",
        "line": 10,
        "character": 24,
    }
    base.update(overrides)
    with pytest.raises(error):
        NavigationRequest(**base)


def test_navigation_request_requires_direction_only_for_calls(
    repository: Path,
) -> None:
    scope = _scope(repository)
    with pytest.raises(ValueError, match="direction"):
        NavigationRequest(scope, Capability.CALLS, "pkg/api.py", 10, 24)
    with pytest.raises(ValueError, match="direction"):
        NavigationRequest(
            scope, Capability.DEFINITIONS, "pkg/api.py", 10, 24, direction="incoming"
        )
    request = NavigationRequest(
        scope, Capability.CALLS, "pkg/api.py", 10, 24, direction="incoming"
    )
    assert request.direction == "incoming"


def test_navigation_records_are_frozen_and_slotted() -> None:
    provenance = (Provenance("lsp", "pyright", "1.1.411", "provider_reported"),)
    location = NavigationLocation(
        "pkg/api.py",
        PositionRange(0, 4),
        1,
        0,
        None,
        None,
        ResolutionLabel.LSP_CONFIRMED,
        provenance,
    )
    with pytest.raises(FrozenInstanceError):
        location.path = "other"  # type: ignore[misc]
    diagnostic = NavigationDiagnostic(
        "pkg/api.py",
        PositionRange(0, 4),
        DiagnosticSeverity.ERROR,
        "code",
        "message",
        (),
        provenance,
    )
    with pytest.raises(FrozenInstanceError):
        diagnostic.message = "other"  # type: ignore[misc]


def test_navigation_tuple_fields_are_deeply_immutable(
    repository: Path,
) -> None:
    scope = _scope(repository)
    provenance = Provenance("lsp", "pyright", "1.1.411", "provider_reported")
    location = NavigationLocation(
        "pkg/api.py",
        PositionRange(0, 4),
        1,
        0,
        None,
        None,
        ResolutionLabel.LSP_CONFIRMED,
        [provenance],  # type: ignore[arg-type]
    )
    diagnostic = NavigationDiagnostic(
        "pkg/api.py",
        PositionRange(0, 4),
        DiagnosticSeverity.ERROR,
        None,
        "message",
        [location],  # type: ignore[arg-type]
        [provenance],  # type: ignore[arg-type]
    )
    result = NavigationResult(
        NavigationStatus.PARTIAL,
        Capability.DIAGNOSTICS,
        Capability.DIAGNOSTICS,
        "pyright",
        "1.1.411",
        scope.repository_id,
        scope.checkout_id,
        "abc",
        "abc",
        1,
        PositionEncoding.UTF8,
        "query_ready",
        None,
        1,
        0,
        10,
        [location],  # type: ignore[arg-type]
        [diagnostic],  # type: ignore[arg-type]
        None,
        ResolutionLabel.LSP_CONFIRMED,
        [provenance],  # type: ignore[arg-type]
        ["partial"],  # type: ignore[arg-type]
    )
    assert location.provenance == (provenance,)
    assert diagnostic.related == (location,)
    assert diagnostic.provenance == (provenance,)
    assert result.locations == (location,)
    assert result.diagnostics == (diagnostic,)
    assert result.provenance == (provenance,)
    assert result.warnings == ("partial",)


@pytest.mark.parametrize(
    "invalid_record",
    [
        "provenance_scalar",
        "location_scalar",
        "location_provenance",
        "diagnostic_related",
        "diagnostic_provenance",
        "result_status",
        "result_locations",
        "result_diagnostics",
        "result_provenance",
        "result_warnings",
    ],
)
def test_public_navigation_records_validate_nested_values(
    repository: Path,
    invalid_record: str,
) -> None:
    scope = _scope(repository)
    provenance = Provenance("lsp", "pyright", "1.1.411", "provider_reported")
    location = NavigationLocation(
        "pkg/api.py",
        PositionRange(0, 4),
        1,
        0,
        None,
        None,
        ResolutionLabel.LSP_CONFIRMED,
        (provenance,),
    )
    diagnostic = NavigationDiagnostic(
        "pkg/api.py",
        PositionRange(0, 4),
        DiagnosticSeverity.ERROR,
        None,
        "message",
        (location,),
        (provenance,),
    )
    result_values = {
        "status": NavigationStatus.PARTIAL,
        "requested_capability": Capability.DIAGNOSTICS,
        "effective_capability": Capability.DIAGNOSTICS,
        "provider": "pyright",
        "provider_version": "1.1.411",
        "repository_id": scope.repository_id,
        "checkout_id": scope.checkout_id,
        "workspace_revision_before": "abc",
        "workspace_revision_after": "abc",
        "document_version": 1,
        "position_encoding": PositionEncoding.UTF8,
        "readiness": "query_ready",
        "symbol": None,
        "total": 1,
        "offset": 0,
        "limit": 10,
        "locations": (location,),
        "diagnostics": (diagnostic,),
        "hover": None,
        "resolution": ResolutionLabel.LSP_CONFIRMED,
        "provenance": (provenance,),
        "warnings": ("partial",),
    }
    with pytest.raises(TypeError):
        if invalid_record == "provenance_scalar":
            Provenance("lsp", object(), "1.1.411", "provider_reported")  # type: ignore[arg-type]
        elif invalid_record == "location_scalar":
            NavigationLocation(  # type: ignore[arg-type]
                1,
                PositionRange(0, 4),
                1,
                0,
                None,
                None,
                ResolutionLabel.LSP_CONFIRMED,
                (provenance,),
            )
        elif invalid_record == "location_provenance":
            NavigationLocation(
                "pkg/api.py",
                PositionRange(0, 4),
                1,
                0,
                None,
                None,
                ResolutionLabel.LSP_CONFIRMED,
                (object(),),  # type: ignore[arg-type]
            )
        elif invalid_record == "diagnostic_related":
            NavigationDiagnostic(
                "pkg/api.py",
                PositionRange(0, 4),
                DiagnosticSeverity.ERROR,
                None,
                "message",
                (object(),),  # type: ignore[arg-type]
                (provenance,),
            )
        elif invalid_record == "diagnostic_provenance":
            NavigationDiagnostic(
                "pkg/api.py",
                PositionRange(0, 4),
                DiagnosticSeverity.ERROR,
                None,
                "message",
                (location,),
                (object(),),  # type: ignore[arg-type]
            )
        else:
            field = invalid_record.removeprefix("result_")
            result_values[field] = (
                "partial" if field == "status" else (object(),)
            )
            NavigationResult(**result_values)  # type: ignore[arg-type]


@pytest.mark.parametrize("record_kind", ["request", "location", "diagnostic"])
@pytest.mark.parametrize(
    "invalid_path",
    [
        "",
        "pkg\\api.py",
        "/pkg/api.py",
        "C:/pkg/api.py",
        "C:pkg/api.py",
        "//server/share.py",
        "./pkg/api.py",
        "pkg/./api.py",
        "pkg/../api.py",
        "pkg//api.py",
        "pkg/api.py/",
        "pkg/\0api.py",
        "pkg/\napi.py",
        "pkg/cafe\u0301.py",
        "pkg/api.py:stream",
        "pkg/trailing.",
        "pkg/trailing ",
        "pkg/CON.py",
        "pkg/con.txt",
        "pkg/COM1.py",
        "a" * 256 + "/api.py",
        "😀" * 64 + "/api.py",
        "a/" * 256 + "api.py",
    ],
)
def test_public_navigation_paths_require_canonical_nfc_posix_relative_text(
    repository: Path,
    record_kind: str,
    invalid_path: str,
) -> None:
    scope = _scope(repository)
    provenance = (Provenance("lsp", "pyright", "1.1.411", "provider_reported"),)
    with pytest.raises(ValueError):
        if record_kind == "request":
            NavigationRequest(
                scope, Capability.DEFINITIONS, invalid_path, 1, 0
            )
        elif record_kind == "location":
            NavigationLocation(
                invalid_path,
                PositionRange(0, 1),
                1,
                0,
                None,
                None,
                ResolutionLabel.LSP_CONFIRMED,
                provenance,
            )
        else:
            NavigationDiagnostic(
                invalid_path,
                PositionRange(0, 1),
                DiagnosticSeverity.ERROR,
                None,
                "message",
                (),
                provenance,
            )


def test_public_navigation_paths_accept_canonical_nfc_posix_relative_text(
    repository: Path,
) -> None:
    scope = _scope(repository)
    path = "pkg/caf\u00e9.py"
    provenance = (Provenance("lsp", "pyright", "1.1.411", "provider_reported"),)
    request = NavigationRequest(scope, Capability.DEFINITIONS, path, 1, 0)
    location = NavigationLocation(
        path,
        PositionRange(0, 1),
        1,
        0,
        None,
        None,
        ResolutionLabel.LSP_CONFIRMED,
        provenance,
    )
    diagnostic = NavigationDiagnostic(
        path,
        PositionRange(0, 1),
        DiagnosticSeverity.ERROR,
        None,
        "message",
        (),
        provenance,
    )
    assert request.path == location.path == diagnostic.path == path


def test_navigation_result_is_frozen(
    repository: Path,
) -> None:
    scope = _scope(repository)
    result = NavigationResult(
        NavigationStatus.OK,
        Capability.DEFINITIONS,
        Capability.DEFINITIONS,
        "pyright",
        "1.1.411",
        scope.repository_id,
        scope.checkout_id,
        "abc",
        "abc",
        1,
        PositionEncoding.UTF8,
        "query_ready",
        None,
        0,
        0,
        10,
        (),
        (),
        None,
        ResolutionLabel.UNRESOLVED,
        (),
        (),
    )
    with pytest.raises(FrozenInstanceError):
        result.status = NavigationStatus.ERROR  # type: ignore[misc]


def test_code_navigation_rejects_foreign_repository(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    tmp_path: Path,
) -> None:
    from tests.code_kernel_helpers import create_python_repository

    other = create_python_repository(tmp_path / "other")
    other_scope = resolve_repository_scope(other)
    session = PyrightSession(
        resolve_repository_scope(repository),
        semantic_pyright.identity,  # type: ignore[arg-type]
        state_root=state_root,
    )
    navigation = CodeNavigation(
        resolve_repository_scope(repository),
        session,
        semantic_pyright.identity,  # type: ignore[arg-type]
    )
    try:
        request = NavigationRequest(
            other_scope, Capability.DEFINITIONS, "pkg/api.py", 1, 0
        )
        with pytest.raises(ValueError, match="navigation repository"):
            navigation.query(request, deadline=time.monotonic() + 5)
    finally:
        navigation.close(deadline=time.monotonic() + 5)


def test_code_navigation_type_checks_constructor(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    scope = resolve_repository_scope(repository)
    with pytest.raises(TypeError):
        CodeNavigation(  # type: ignore[arg-type]
            "not-a-scope",
            semantic_pyright.identity,
            semantic_pyright.identity,  # type: ignore[arg-type]
        )
    session = PyrightSession(
        scope,
        semantic_pyright.identity,  # type: ignore[arg-type]
        state_root=state_root,
    )
    with pytest.raises(TypeError):
        CodeNavigation(scope, "not-a-session", semantic_pyright.identity)  # type: ignore[arg-type]
    session.close(deadline=time.monotonic() + 5)


def test_code_navigation_binds_exact_session_identity_and_repository(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    tmp_path: Path,
) -> None:
    from tests.code_kernel_helpers import create_python_repository

    scope = resolve_repository_scope(repository)
    session = PyrightSession(scope, semantic_pyright.identity, state_root=state_root)
    other = create_python_repository(tmp_path / "other-navigation-repository")
    other_scope = resolve_repository_scope(other)
    mismatched_identity = replace(semantic_pyright.identity, source="other-source")
    try:
        with pytest.raises(ValueError, match="identity"):
            CodeNavigation(scope, session, mismatched_identity)
        with pytest.raises(ValueError, match="repository"):
            CodeNavigation(other_scope, session, semantic_pyright.identity)
    finally:
        session.close(deadline=time.monotonic() + 5)


def _graph_location(path: str, start: int, end: int) -> NavigationLocation:
    return NavigationLocation(
        path,
        PositionRange(start, end),
        1,
        0,
        None,
        None,
        ResolutionLabel.GRAPH_CANDIDATE,
        (Provenance("graph", "evidence-graph", "structural", "name_resolution"),),
    )


def _navigation(
    repository: Path,
    state_root: Path,
    fixture: SemanticPyrightFixture,
    *,
    structural=None,
    resolver=None,
    edge=None,
) -> tuple[CodeNavigation, PyrightSession]:
    scope = resolve_repository_scope(repository)
    session = PyrightSession(scope, fixture.identity, state_root=state_root)
    navigation = CodeNavigation(
        scope,
        session,
        fixture.identity,
        structural_candidates=structural,
        symbol_resolver=resolver,
        edge_verifier=edge,
    )
    return navigation, session


def _open_document(scope: RepositoryScope, path: str, *, version: int = 1) -> OpenDocument:
    source = resolve_repository_source(scope, path)
    content = source.absolute_path.read_bytes()
    return OpenDocument(source, content, hashlib.sha256(content).hexdigest(), version)


def _source_anchor(
    scope: RepositoryScope,
    path: str,
    line: int,
    character: int,
) -> SourceAnchor:
    document = _open_document(scope, path)
    return SourceDocument.from_bytes(path, document.content).validate_anchor(
        line=line,
        character=character,
    )


def _revision(repository: Path, marker: str) -> WorkspaceRevision:
    revision = compute_workspace_revision(
        resolve_repository_scope(repository), deadline=time.monotonic() + 5
    )
    return replace(revision, revision_sha256=marker * 64)


def test_post_revision_reuses_exact_verified_revision(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    events: list[str] = []
    monkeypatch.setattr(
        code_navigation,
        "verify_workspace_revision_unchanged",
        lambda value, revision, *, deadline: events.append("verify") or True,
        raising=False,
    )
    monkeypatch.setattr(
        code_navigation,
        "_compute_revision",
        lambda value, *, deadline: (_ for _ in ()).throw(AssertionError("full recompute")),
    )

    current = code_navigation._compute_post_revision(
        scope,
        expected,
        deadline=time.monotonic() + 5,
    )

    assert current is expected
    assert events == ["verify"]


def test_post_revision_mismatch_falls_back_to_full_recomputation(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    current = replace(expected, revision_sha256="f" * 64)
    monkeypatch.setattr(
        code_navigation,
        "verify_workspace_revision_unchanged",
        lambda value, revision, *, deadline: False,
        raising=False,
    )
    monkeypatch.setattr(
        code_navigation,
        "_compute_revision",
        lambda value, *, deadline: current,
    )

    assert (
        code_navigation._compute_post_revision(
            scope,
            expected,
            deadline=time.monotonic() + 5,
        )
        is current
    )


def test_stable_queries_reuse_content_addressed_source_documents(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    request_document = _open_document(scope, "pkg/service.py")
    target = resolve_repository_source(scope, "pkg/api.py")
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    _patch_stable_attempt(monkeypatch, session, revision, request_document)
    monkeypatch.setattr(
        session,
        "definition",
        lambda anchor, *, deadline: ProviderLocations(
            (
                LspLocation(
                    target.uri,
                    LspRange(LspPosition(1, 8), LspPosition(1, 14)),
                ),
            ),
            "provider_reported",
            False,
        ),
    )
    real_from_bytes = SourceDocument.from_bytes
    parsed: list[str] = []

    def recording_from_bytes(
        cls: type[SourceDocument],
        path: str,
        content: bytes,
    ) -> SourceDocument:
        parsed.append(path)
        return real_from_bytes(path, content)

    monkeypatch.setattr(SourceDocument, "from_bytes", classmethod(recording_from_bytes))
    request = NavigationRequest(
        scope,
        Capability.DEFINITIONS,
        "pkg/service.py",
        10,
        20,
    )
    try:
        first = navigation.query(request, deadline=time.monotonic() + 5)
        second = navigation.query(request, deadline=time.monotonic() + 5)

        assert first.status is NavigationStatus.OK
        assert second.status is NavigationStatus.OK
        assert parsed.count("pkg/service.py") == 1
        assert parsed.count("pkg/api.py") == 1
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_source_document_cache_lru_tracks_only_consumed_documents(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = tuple(f"pkg/cache_{index}.py" for index in range(4))
    for index, path in enumerate(paths):
        (repository / path).write_text(f"value = {index}\n", encoding="utf-8")
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    open_documents = {path: _open_document(scope, path) for path in paths}
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    _patch_stable_attempt(monkeypatch, session, revision, open_documents[paths[0]])
    monkeypatch.setattr(code_navigation, "_MAX_SOURCE_DOCUMENT_CACHE_ENTRIES", 3)
    monkeypatch.setattr(
        session,
        "open_document",
        lambda path, *, deadline: open_documents[path],
    )
    monkeypatch.setattr(
        session,
        "definition",
        lambda anchor, *, deadline: ProviderLocations(
            (), "provider_reported", False
        ),
    )
    real_from_bytes = SourceDocument.from_bytes
    parsed: list[str] = []

    def recording_from_bytes(
        cls: type[SourceDocument],
        path: str,
        content: bytes,
    ) -> SourceDocument:
        parsed.append(path)
        return real_from_bytes(path, content)

    monkeypatch.setattr(SourceDocument, "from_bytes", classmethod(recording_from_bytes))

    def query(path: str) -> None:
        result = navigation.query(
            NavigationRequest(scope, Capability.DEFINITIONS, path, 1, 0),
            deadline=time.monotonic() + 5,
        )
        assert result.status is NavigationStatus.OK

    try:
        for path in paths[:3]:
            query(path)
        query(paths[0])
        query(paths[3])
        query(paths[1])
        query(paths[0])

        assert parsed.count(paths[0]) == 1
        assert parsed.count(paths[1]) == 2
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_source_document_cache_repeated_use_refreshes_attempt_recency(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = tuple(f"pkg/repeated_{name}.py" for name in ("a", "b", "c"))
    for path in paths:
        (repository / path).write_text("value = 1\n", encoding="utf-8")
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    request_document = _open_document(scope, paths[0])
    sources = {path: resolve_repository_source(scope, path) for path in paths}
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    _patch_stable_attempt(monkeypatch, session, revision, request_document)
    monkeypatch.setattr(code_navigation, "_MAX_SOURCE_DOCUMENT_CACHE_ENTRIES", 2)
    target_range = LspRange(LspPosition(0, 0), LspPosition(0, 1))
    responses = iter(
        (
            tuple(
                LspLocation(sources[path].uri, target_range)
                for path in (paths[1], paths[0], paths[2])
            ),
            (LspLocation(sources[paths[1]].uri, target_range),),
        )
    )
    monkeypatch.setattr(
        session,
        "definition",
        lambda anchor, *, deadline: ProviderLocations(
            next(responses), "provider_reported", False
        ),
    )
    real_from_bytes = SourceDocument.from_bytes
    parsed: list[str] = []

    def recording_from_bytes(
        cls: type[SourceDocument],
        path: str,
        content: bytes,
    ) -> SourceDocument:
        parsed.append(path)
        return real_from_bytes(path, content)

    monkeypatch.setattr(SourceDocument, "from_bytes", classmethod(recording_from_bytes))
    request = NavigationRequest(scope, Capability.DEFINITIONS, paths[0], 1, 0)
    try:
        first = navigation.query(request, deadline=time.monotonic() + 5)
        second = navigation.query(request, deadline=time.monotonic() + 5)

        assert first.status is NavigationStatus.OK
        assert second.status is NavigationStatus.OK
        assert parsed.count(paths[0]) == 1
        assert parsed.count(paths[1]) == 2
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_source_document_cache_rejects_newline_dense_document_over_byte_bound(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = "pkg/newline_dense.py"
    (repository / path).write_bytes(b"x\n" * 8)
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    request_document = _open_document(scope, path)
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    _patch_stable_attempt(monkeypatch, session, revision, request_document)
    monkeypatch.setattr(code_navigation, "_MAX_SOURCE_DOCUMENT_CACHE_BYTES", 1024)
    monkeypatch.setattr(
        session,
        "definition",
        lambda anchor, *, deadline: ProviderLocations(
            (), "provider_reported", False
        ),
    )
    real_from_bytes = SourceDocument.from_bytes
    parsed: list[str] = []

    def recording_from_bytes(
        cls: type[SourceDocument],
        parsed_path: str,
        content: bytes,
    ) -> SourceDocument:
        parsed.append(parsed_path)
        return real_from_bytes(parsed_path, content)

    monkeypatch.setattr(SourceDocument, "from_bytes", classmethod(recording_from_bytes))
    request = NavigationRequest(scope, Capability.DEFINITIONS, path, 1, 0)
    try:
        first = navigation.query(request, deadline=time.monotonic() + 5)
        second = navigation.query(request, deadline=time.monotonic() + 5)

        assert first.status is NavigationStatus.OK
        assert second.status is NavigationStatus.OK
        assert parsed.count(path) == 2
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_source_document_cache_rejects_path_and_key_heavy_document_over_byte_bound(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = f"pkg/retained_{'x' * 80}.py"
    (repository / path).write_text("value = 1\n", encoding="utf-8")
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    request_document = _open_document(scope, path)
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    _patch_stable_attempt(monkeypatch, session, revision, request_document)
    monkeypatch.setattr(code_navigation, "_MAX_SOURCE_DOCUMENT_CACHE_BYTES", 1600)
    monkeypatch.setattr(
        session,
        "definition",
        lambda anchor, *, deadline: ProviderLocations(
            (), "provider_reported", False
        ),
    )
    real_from_bytes = SourceDocument.from_bytes
    parsed: list[str] = []

    def recording_from_bytes(
        cls: type[SourceDocument],
        parsed_path: str,
        content: bytes,
    ) -> SourceDocument:
        parsed.append(parsed_path)
        return real_from_bytes(parsed_path, content)

    monkeypatch.setattr(SourceDocument, "from_bytes", classmethod(recording_from_bytes))
    request = NavigationRequest(scope, Capability.DEFINITIONS, path, 1, 0)
    try:
        first = navigation.query(request, deadline=time.monotonic() + 5)
        second = navigation.query(request, deadline=time.monotonic() + 5)

        assert first.status is NavigationStatus.OK
        assert second.status is NavigationStatus.OK
        assert parsed.count(path) == 2
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_source_document_cache_concurrent_publication_preserves_access_order(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from code_navigation import _AttemptDocuments

    paths = tuple(f"pkg/concurrent_{name}.py" for name in ("a", "b", "c"))
    for path in paths:
        (repository / path).write_text("value = 1\n", encoding="utf-8")
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    revision_entries = {entry.path: entry for entry in revision.entries}
    sources = {path: resolve_repository_source(scope, path) for path in paths}
    documents = {
        path: SourceDocument.from_bytes(path, (repository / path).read_bytes())
        for path in paths
    }
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    monkeypatch.setattr(code_navigation, "_MAX_SOURCE_DOCUMENT_CACHE_ENTRIES", 2)
    initial = _AttemptDocuments()
    for path in paths[:2]:
        initial[sources[path].uri] = documents[path]
        initial.consume(sources[path].uri, documents[path])
    navigation._publish_source_documents(revision_entries, initial)

    slow = navigation._seed_source_documents(revision_entries)
    fast = navigation._seed_source_documents(revision_entries)
    slow.consume(sources[paths[0]].uri, documents[paths[0]])
    fast.consume(sources[paths[1]].uri, documents[paths[1]])
    fast[sources[paths[2]].uri] = documents[paths[2]]
    fast.consume(sources[paths[2]].uri, documents[paths[2]])
    try:
        navigation._publish_source_documents(revision_entries, fast)
        navigation._publish_source_documents(revision_entries, slow)

        current = navigation._seed_source_documents(revision_entries)
        assert sources[paths[0]].uri not in current
        assert sources[paths[1]].uri in current
        assert sources[paths[2]].uri in current
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_source_document_cache_consume_after_eviction_republishes_when_stable(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from code_navigation import _AttemptDocuments

    paths = tuple(f"pkg/inverse_{name}.py" for name in ("a", "b", "c"))
    for path in paths:
        (repository / path).write_text("value = 1\n", encoding="utf-8")
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    revision_entries = {entry.path: entry for entry in revision.entries}
    sources = {path: resolve_repository_source(scope, path) for path in paths}
    documents = {
        path: SourceDocument.from_bytes(path, (repository / path).read_bytes())
        for path in paths
    }
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    monkeypatch.setattr(code_navigation, "_MAX_SOURCE_DOCUMENT_CACHE_ENTRIES", 2)
    initial = _AttemptDocuments()
    for path in paths[:2]:
        initial[sources[path].uri] = documents[path]
        initial.consume(sources[path].uri, documents[path])
    navigation._publish_source_documents(revision_entries, initial)

    local = navigation._seed_source_documents(revision_entries)
    other = navigation._seed_source_documents(revision_entries)
    other.consume(sources[paths[1]].uri, documents[paths[1]])
    other[sources[paths[2]].uri] = documents[paths[2]]
    other.consume(sources[paths[2]].uri, documents[paths[2]])
    try:
        navigation._publish_source_documents(revision_entries, other)
        local.consume(sources[paths[0]].uri, documents[paths[0]])
        navigation._publish_source_documents(revision_entries, local)

        current = navigation._seed_source_documents(revision_entries)
        assert sources[paths[0]].uri in current
        assert sources[paths[1]].uri not in current
        assert sources[paths[2]].uri in current
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_source_document_cache_structural_alias_refreshes_recency(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = tuple(f"pkg/alias_{name}.py" for name in ("a", "b", "c"))
    for path in paths:
        (repository / path).write_text("value = 1\n", encoding="utf-8")
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    request_document = _open_document(scope, paths[0])
    responses = iter(
        (
            tuple(
                _graph_location(path, 0, 1)
                for path in (paths[0], paths[1], paths[0], paths[2])
            ),
            (_graph_location(paths[1], 0, 1),),
        )
    )
    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        structural=lambda request, deadline: next(responses),
    )
    _patch_stable_attempt(monkeypatch, session, revision, request_document)
    monkeypatch.setattr(code_navigation, "_MAX_SOURCE_DOCUMENT_CACHE_ENTRIES", 2)
    monkeypatch.setattr(
        session,
        "definition",
        lambda anchor, *, deadline: ProviderLocations(
            (), "provider_reported", False
        ),
    )
    real_from_bytes = SourceDocument.from_bytes
    parsed: list[str] = []

    def recording_from_bytes(
        cls: type[SourceDocument],
        path: str,
        content: bytes,
    ) -> SourceDocument:
        parsed.append(path)
        return real_from_bytes(path, content)

    monkeypatch.setattr(SourceDocument, "from_bytes", classmethod(recording_from_bytes))
    request = NavigationRequest(scope, Capability.DEFINITIONS, paths[0], 1, 0)
    try:
        first = navigation.query(request, deadline=time.monotonic() + 5)
        second = navigation.query(request, deadline=time.monotonic() + 5)

        assert first.status is NavigationStatus.PARTIAL
        assert second.status is NavigationStatus.PARTIAL
        assert parsed.count(paths[0]) == 1
        assert parsed.count(paths[1]) == 2
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_code_navigation_close_clears_source_cache_when_session_close_fails(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    request_document = _open_document(scope, "pkg/service.py")
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    _patch_stable_attempt(monkeypatch, session, revision, request_document)
    monkeypatch.setattr(
        session,
        "definition",
        lambda anchor, *, deadline: ProviderLocations(
            (), "provider_reported", False
        ),
    )
    result = navigation.query(
        NavigationRequest(
            scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
        ),
        deadline=time.monotonic() + 5,
    )
    assert result.status is NavigationStatus.OK
    assert navigation._source_document_cache
    real_close = session.close
    monkeypatch.setattr(
        session,
        "close",
        lambda *, deadline: (_ for _ in ()).throw(RuntimeError("close failed")),
    )
    try:
        with pytest.raises(RuntimeError, match="close failed"):
            navigation.close(deadline=time.monotonic() + 5)
        assert not navigation._source_document_cache
        assert navigation._source_document_cache_bytes == 0
    finally:
        real_close(deadline=time.monotonic() + 5)


def _patch_stable_attempt(
    monkeypatch: pytest.MonkeyPatch,
    session: PyrightSession,
    revision: WorkspaceRevision,
    document: OpenDocument,
    *,
    encoding: PositionEncoding = PositionEncoding.UTF8,
) -> None:
    session._position_encoding = encoding
    monkeypatch.setattr(
        code_navigation,
        "_compute_revision",
        lambda repository, *, deadline: revision,
    )
    monkeypatch.setattr(session, "synchronize", lambda value, *, deadline: None)
    monkeypatch.setattr(
        session,
        "open_document",
        lambda path, *, deadline: document,
    )


def test_query_validates_request_before_expired_deadline(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    try:
        with pytest.raises(TypeError, match="NavigationRequest"):
            navigation.query(object(), deadline=0.0)  # type: ignore[arg-type]
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_query_rejects_empty_revision_before_provider_operation(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = replace(_revision(repository, "a"), revision_sha256="")
    document = _open_document(scope, "pkg/service.py")
    provider_calls = 0
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    _patch_stable_attempt(monkeypatch, session, revision, document)

    def definition(anchor: SourceAnchor, *, deadline: float) -> ProviderLocations:
        nonlocal provider_calls
        provider_calls += 1
        return ProviderLocations((), "provider_reported", False)

    monkeypatch.setattr(session, "definition", definition)
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
            ),
            deadline=time.monotonic() + 5,
        )
        assert result.status is NavigationStatus.ERROR
        assert result.locations == ()
        assert provider_calls == 0
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_query_attempt_order_keeps_structural_callback_and_target_reads_in_fence(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    request_document = _open_document(scope, "pkg/service.py")
    target = resolve_repository_source(scope, "pkg/api.py")
    events: list[str] = []

    def structural(
        request: NavigationRequest, deadline: float
    ) -> tuple[NavigationLocation, ...]:
        assert request.path == "pkg/service.py"
        assert deadline == absolute_deadline
        events.append("structural")
        return ()

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        structural=structural,
    )
    session._position_encoding = PositionEncoding.UTF8
    original_read = __import__("bounded_io").read_stable_bytes
    revision_calls = 0

    def compute(_repository: RepositoryScope, *, deadline: float) -> WorkspaceRevision:
        nonlocal revision_calls
        revision_calls += 1
        events.append("pre" if revision_calls == 1 else "post")
        return revision

    def synchronize(value: WorkspaceRevision, *, deadline: float) -> None:
        assert value is revision
        events.append("synchronize")

    def open_document(path: str, *, deadline: float) -> OpenDocument:
        assert path == request_document.source.relative_path
        events.append("open")
        return request_document

    def definition(_anchor, *, deadline: float) -> ProviderLocations:
        events.append("provider")
        return ProviderLocations(
            (
                LspLocation(
                    target.uri,
                    LspRange(LspPosition(1, 8), LspPosition(1, 14)),
                ),
            ),
            "provider_reported",
            False,
        )

    def read_target(path: Path, max_bytes: int, **kwargs) -> bytes:
        events.append(f"read:{path.relative_to(repository).as_posix()}")
        return original_read(path, max_bytes, **kwargs)

    monkeypatch.setattr(code_navigation, "_compute_revision", compute)
    monkeypatch.setattr(code_navigation, "read_stable_bytes", read_target, raising=False)
    monkeypatch.setattr(session, "synchronize", synchronize)
    monkeypatch.setattr(session, "open_document", open_document)
    monkeypatch.setattr(session, "definition", definition)
    absolute_deadline = time.monotonic() + 5
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
            ),
            deadline=absolute_deadline,
        )
        assert result.status is NavigationStatus.OK
        assert events == [
            "pre",
            "synchronize",
            "read:pkg/service.py",
            "open",
            "provider",
            "structural",
            "read:pkg/api.py",
            "post",
        ]
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_provider_return_after_deadline_prevents_structural_callback(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    clock = {"now": 1.0}
    real_monotonic = time.monotonic
    structural_calls = 0

    def definition(anchor: SourceAnchor, *, deadline: float) -> ProviderLocations:
        clock["now"] = 11.0
        return ProviderLocations((), "provider_reported", False)

    def structural(
        request: NavigationRequest, deadline: float
    ) -> tuple[NavigationLocation, ...]:
        nonlocal structural_calls
        structural_calls += 1
        return (_graph_location("pkg/api.py", 6, 15),)

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        structural=structural,
    )
    _patch_stable_attempt(monkeypatch, session, revision, document)
    monkeypatch.setattr(code_navigation.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(session, "definition", definition)
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
            ),
            deadline=10.0,
        )
        assert structural_calls == 0
        assert result.status is NavigationStatus.TIMEOUT
        assert result.locations == ()
        assert result.provenance == ()
    finally:
        monkeypatch.setattr(code_navigation.time, "monotonic", real_monotonic)
        session.close(deadline=real_monotonic() + 5)


@pytest.mark.parametrize("callback_kind", ["structural", "resolver", "edge"])
def test_callback_return_after_deadline_never_publishes_facts(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
    callback_kind: str,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    clock = {"now": 1.0}
    real_monotonic = time.monotonic

    def callback(*args):
        clock["now"] = 11.0
        if callback_kind == "edge":
            return True
        return (_graph_location("pkg/api.py", 6, 15),)

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        structural=callback if callback_kind == "structural" else None,
        resolver=callback if callback_kind == "resolver" else None,
        edge=callback if callback_kind == "edge" else None,
    )
    monkeypatch.setattr(
        code_navigation,
        "_compute_revision",
        lambda repository, *, deadline: revision,
    )
    monkeypatch.setattr(code_navigation.time, "monotonic", lambda: clock["now"])
    try:
        if callback_kind == "structural":
            session._position_encoding = PositionEncoding.UTF8
            monkeypatch.setattr(
                session, "synchronize", lambda value, *, deadline: None
            )
            monkeypatch.setattr(
                session, "open_document", lambda path, *, deadline: document
            )
            monkeypatch.setattr(
                session,
                "definition",
                lambda anchor, *, deadline: ProviderLocations(
                    (), "provider_reported", False
                ),
            )
            result = navigation.query(
                NavigationRequest(
                    scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
                ),
                deadline=10.0,
            )
        elif callback_kind == "resolver":
            result = navigation.resolve_symbol(
                "PublicApi", repository=scope, deadline=10.0
            )
        else:
            result = navigation.verify_edge(
                _source_anchor(scope, "pkg/service.py", 10, 15),
                _source_anchor(scope, "pkg/api.py", 2, 8),
                repository=scope,
                deadline=10.0,
            )
        assert result.status is NavigationStatus.TIMEOUT
        assert result.locations == ()
        assert result.provenance == ()
    finally:
        monkeypatch.setattr(code_navigation.time, "monotonic", real_monotonic)
        session.close(deadline=real_monotonic() + 5)


@pytest.mark.parametrize(
    "crossing_stage",
    ["stable_read", "source_document", "lsp_range", "byte_position"],
)
def test_expensive_conversion_crossing_deadline_never_publishes_facts(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
    crossing_stage: str,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    target = resolve_repository_source(scope, "pkg/api.py")
    clock = {"now": 1.0}
    real_monotonic = time.monotonic
    provider_calls = 0
    structural = None

    if crossing_stage == "stable_read":
        original_read = code_navigation.read_stable_bytes

        def read_target(path: Path, max_bytes: int, **kwargs) -> bytes:
            content = original_read(path, max_bytes, **kwargs)
            if Path(path) == target.absolute_path:
                clock["now"] = 11.0
            return content

        monkeypatch.setattr(code_navigation, "read_stable_bytes", read_target)
    elif crossing_stage == "source_document":
        original_from_bytes = SourceDocument.from_bytes

        def from_bytes(cls, path: str, content: bytes) -> SourceDocument:
            value = original_from_bytes(path, content)
            clock["now"] = 11.0
            return value

        monkeypatch.setattr(SourceDocument, "from_bytes", classmethod(from_bytes))
    elif crossing_stage == "lsp_range":
        original_to_byte_range = SourceDocument.to_byte_range

        def to_byte_range(self, value, encoding):
            result = original_to_byte_range(self, value, encoding)
            clock["now"] = 11.0
            return result

        monkeypatch.setattr(SourceDocument, "to_byte_range", to_byte_range)
    else:
        original_byte_position = code_navigation._byte_position

        def byte_position(document: SourceDocument, byte_offset: int):
            result = original_byte_position(document, byte_offset)
            clock["now"] = 11.0
            return result

        monkeypatch.setattr(code_navigation, "_byte_position", byte_position)

        def structural_candidates(request, deadline):
            return (_graph_location("pkg/api.py", 6, 15),)

        structural = structural_candidates

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        structural=structural,
    )
    session._position_encoding = PositionEncoding.UTF8
    monkeypatch.setattr(
        code_navigation,
        "_compute_revision",
        lambda repository, *, deadline: revision,
    )
    monkeypatch.setattr(session, "synchronize", lambda value, *, deadline: None)
    monkeypatch.setattr(
        session, "open_document", lambda path, *, deadline: document
    )

    def definition(anchor: SourceAnchor, *, deadline: float) -> ProviderLocations:
        nonlocal provider_calls
        provider_calls += 1
        return ProviderLocations(
            (
                LspLocation(
                    target.uri,
                    LspRange(LspPosition(0, 6), LspPosition(0, 15)),
                ),
            )
            if crossing_stage in {"stable_read", "lsp_range"}
            else (),
            "provider_reported",
            False,
        )

    monkeypatch.setattr(session, "definition", definition)
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
            ),
            deadline=10.0,
        )
        assert result.status is NavigationStatus.TIMEOUT
        assert result.locations == ()
        assert result.provenance == ()
        if crossing_stage == "source_document":
            assert provider_calls == 0
    finally:
        monkeypatch.setattr(code_navigation.time, "monotonic", real_monotonic)
        session.close(deadline=real_monotonic() + 5)


@pytest.mark.parametrize("operation", ["query", "resolve_symbol", "verify_edge"])
def test_result_construction_crossing_deadline_never_publishes_facts(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    target = resolve_repository_source(scope, "pkg/api.py")
    clock = {"now": 1.0}
    real_monotonic = time.monotonic
    original_post_init = NavigationResult.__post_init__

    def post_init(result: NavigationResult) -> None:
        original_post_init(result)
        if result.locations:
            clock["now"] = 11.0

    def resolver(*args):
        return (_graph_location("pkg/api.py", 6, 15),)

    def verifier(*args):
        return True

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        resolver=resolver if operation == "resolve_symbol" else None,
        edge=verifier if operation == "verify_edge" else None,
    )
    monkeypatch.setattr(
        code_navigation,
        "_compute_revision",
        lambda repository, *, deadline: revision,
    )
    monkeypatch.setattr(code_navigation.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(NavigationResult, "__post_init__", post_init)
    try:
        if operation == "query":
            _patch_stable_attempt(monkeypatch, session, revision, document)
            monkeypatch.setattr(
                session,
                "definition",
                lambda anchor, *, deadline: ProviderLocations(
                    (
                        LspLocation(
                            target.uri,
                            LspRange(LspPosition(0, 6), LspPosition(0, 15)),
                        ),
                    ),
                    "provider_reported",
                    False,
                ),
            )
            result = navigation.query(
                NavigationRequest(
                    scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
                ),
                deadline=10.0,
            )
        elif operation == "resolve_symbol":
            result = navigation.resolve_symbol(
                "PublicApi", repository=scope, deadline=10.0
            )
        else:
            result = navigation.verify_edge(
                _source_anchor(scope, "pkg/service.py", 10, 15),
                _source_anchor(scope, "pkg/api.py", 2, 8),
                repository=scope,
                deadline=10.0,
            )
        assert result.status is NavigationStatus.TIMEOUT
        assert result.locations == ()
        assert result.provenance == ()
    finally:
        monkeypatch.setattr(code_navigation.time, "monotonic", real_monotonic)
        session.close(deadline=real_monotonic() + 5)


@pytest.mark.parametrize("mutate_every_attempt", [False, True])
def test_provider_edit_race_retries_whole_attempt_once_and_never_publishes_stale_facts(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
    mutate_every_attempt: bool,
) -> None:
    scope = resolve_repository_scope(repository)
    revisions = (_revision(repository, "a"), _revision(repository, "b"), _revision(repository, "c"))
    request_document = _open_document(scope, "pkg/service.py")
    target = resolve_repository_source(scope, "pkg/api.py")
    current_revision = 0
    provider_calls = 0
    synchronize_calls = 0

    def compute(_repository: RepositoryScope, *, deadline: float) -> WorkspaceRevision:
        return revisions[current_revision]

    def synchronize(_revision: WorkspaceRevision, *, deadline: float) -> None:
        nonlocal synchronize_calls
        synchronize_calls += 1

    def definition(_anchor, *, deadline: float) -> ProviderLocations:
        nonlocal current_revision, provider_calls
        provider_calls += 1
        if provider_calls == 1 or mutate_every_attempt:
            current_revision += 1
        return ProviderLocations(
            (
                LspLocation(
                    target.uri,
                    LspRange(LspPosition(1, 8), LspPosition(1, 14)),
                ),
            ),
            "provider_reported",
            False,
        )

    navigation, session = _navigation(repository, state_root, semantic_pyright)
    session._position_encoding = PositionEncoding.UTF8
    monkeypatch.setattr(code_navigation, "_compute_revision", compute)
    monkeypatch.setattr(session, "synchronize", synchronize)
    monkeypatch.setattr(session, "open_document", lambda path, *, deadline: request_document)
    monkeypatch.setattr(session, "definition", definition)
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
            ),
            deadline=time.monotonic() + 5,
        )
        assert provider_calls == 2
        assert synchronize_calls == 2
        if mutate_every_attempt:
            assert result.status is NavigationStatus.STALE
            assert result.locations == ()
            assert result.diagnostics == ()
            assert result.hover is None
            assert result.provenance == ()
        else:
            assert result.status is NavigationStatus.OK
            assert len(result.locations) == 1
            assert result.workspace_revision_before == revisions[1].revision_sha256
            assert result.workspace_revision_after == revisions[1].revision_sha256
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_target_revision_mismatch_aborts_attempt_before_post_revision(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    revisions = (_revision(repository, "a"), _revision(repository, "b"))
    document = _open_document(scope, "pkg/service.py")
    target = resolve_repository_source(scope, "pkg/api.py")
    original_read = code_navigation.read_stable_bytes
    revision_calls = 0
    provider_calls = 0
    target_reads = 0

    def compute(_repository: RepositoryScope, *, deadline: float) -> WorkspaceRevision:
        nonlocal revision_calls
        revision_calls += 1
        return revisions[0] if revision_calls == 1 else revisions[1]

    def read_target(path: Path, max_bytes: int, **kwargs) -> bytes:
        nonlocal target_reads
        content = original_read(path, max_bytes, **kwargs)
        if Path(path) == target.absolute_path:
            target_reads += 1
            if target_reads == 1:
                return bytes([content[0] ^ 1]) + content[1:]
        return content

    def definition(anchor: SourceAnchor, *, deadline: float) -> ProviderLocations:
        nonlocal provider_calls
        provider_calls += 1
        return ProviderLocations(
            (
                LspLocation(
                    target.uri,
                    LspRange(LspPosition(0, 6), LspPosition(0, 15)),
                ),
            ),
            "provider_reported",
            False,
        )

    navigation, session = _navigation(repository, state_root, semantic_pyright)
    session._position_encoding = PositionEncoding.UTF8
    monkeypatch.setattr(code_navigation, "_compute_revision", compute)
    monkeypatch.setattr(code_navigation, "read_stable_bytes", read_target)
    monkeypatch.setattr(session, "synchronize", lambda value, *, deadline: None)
    monkeypatch.setattr(
        session, "open_document", lambda path, *, deadline: document
    )
    monkeypatch.setattr(session, "definition", definition)
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
            ),
            deadline=time.monotonic() + 5,
        )
        assert provider_calls == 2
        assert target_reads == 2
        assert revision_calls == 3
        assert result.status is NavigationStatus.OK
        assert len(result.locations) == 1
        assert result.workspace_revision_before == revisions[1].revision_sha256
        assert result.workspace_revision_after == revisions[1].revision_sha256
    finally:
        session.close(deadline=time.monotonic() + 5)


@pytest.mark.parametrize("operation", ["provider_target", "structural", "verify_edge"])
@pytest.mark.parametrize("failure_type", [FileNotFoundError, PermissionError, OSError])
@pytest.mark.parametrize("mutate_every_attempt", [False, True])
def test_pre_revision_disk_read_failure_retries_every_fenced_path(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    failure_type: type[OSError],
    mutate_every_attempt: bool,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    target = resolve_repository_source(scope, "pkg/api.py")
    original_read = code_navigation.read_stable_bytes
    target_reads = 0

    def read_target(path: Path, max_bytes: int, **kwargs) -> bytes:
        nonlocal target_reads
        if Path(path) == target.absolute_path:
            target_reads += 1
            if target_reads == 1 or mutate_every_attempt:
                raise failure_type("revision target changed")
        return original_read(path, max_bytes, **kwargs)

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        structural=(
            (lambda request, deadline: (_graph_location("pkg/api.py", 6, 15),))
            if operation == "structural"
            else None
        ),
        edge=(
            (lambda source, target, repository, deadline: True)
            if operation == "verify_edge"
            else None
        ),
    )
    monkeypatch.setattr(
        code_navigation,
        "_compute_revision",
        lambda repository, *, deadline: revision,
    )
    monkeypatch.setattr(code_navigation, "read_stable_bytes", read_target)
    try:
        if operation in {"provider_target", "structural"}:
            session._position_encoding = PositionEncoding.UTF8
            monkeypatch.setattr(
                session, "synchronize", lambda value, *, deadline: None
            )
            monkeypatch.setattr(
                session, "open_document", lambda path, *, deadline: document
            )
            monkeypatch.setattr(
                session,
                "definition",
                lambda anchor, *, deadline: ProviderLocations(
                    (
                        LspLocation(
                            target.uri,
                            LspRange(LspPosition(0, 6), LspPosition(0, 15)),
                        ),
                    )
                    if operation == "provider_target"
                    else (),
                    "provider_reported",
                    False,
                ),
            )
            result = navigation.query(
                NavigationRequest(
                    scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
                ),
                deadline=time.monotonic() + 5,
            )
        else:
            result = navigation.verify_edge(
                _source_anchor(scope, "pkg/service.py", 10, 15),
                _source_anchor(scope, "pkg/api.py", 2, 8),
                repository=scope,
                deadline=time.monotonic() + 5,
            )
        assert target_reads == 2
        if mutate_every_attempt:
            assert result.status is NavigationStatus.STALE
            assert result.locations == ()
            assert result.provenance == ()
        elif operation == "structural":
            assert result.status is NavigationStatus.PARTIAL
            assert len(result.locations) == 1
        else:
            assert result.status is NavigationStatus.OK
            assert len(result.locations) == 1
    finally:
        session.close(deadline=time.monotonic() + 5)


@pytest.mark.parametrize("mutate_every_attempt", [False, True])
def test_query_source_hash_is_checked_before_utf8_decode(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
    mutate_every_attempt: bool,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    clean = _open_document(scope, "pkg/service.py")
    invalid_content = b"\xff" + clean.content[1:]
    invalid = OpenDocument(
        clean.source,
        invalid_content,
        hashlib.sha256(invalid_content).hexdigest(),
        clean.version,
    )
    open_calls = 0
    provider_calls = 0

    def open_document(path: str, *, deadline: float) -> OpenDocument:
        nonlocal open_calls
        open_calls += 1
        return invalid if open_calls == 1 or mutate_every_attempt else clean

    def definition(anchor: SourceAnchor, *, deadline: float) -> ProviderLocations:
        nonlocal provider_calls
        provider_calls += 1
        return ProviderLocations((), "provider_reported", False)

    navigation, session = _navigation(repository, state_root, semantic_pyright)
    session._position_encoding = PositionEncoding.UTF8
    monkeypatch.setattr(
        code_navigation,
        "_compute_revision",
        lambda repository, *, deadline: revision,
    )
    monkeypatch.setattr(session, "synchronize", lambda value, *, deadline: None)
    monkeypatch.setattr(session, "open_document", open_document)
    monkeypatch.setattr(session, "definition", definition)
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
            ),
            deadline=time.monotonic() + 5,
        )
        assert open_calls == 2
        if mutate_every_attempt:
            assert provider_calls == 0
            assert result.status is NavigationStatus.STALE
            assert result.locations == ()
        else:
            assert provider_calls == 1
            assert result.status is NavigationStatus.OK
    finally:
        session.close(deadline=time.monotonic() + 5)


@pytest.mark.parametrize(
    "mismatch",
    ["repository_id", "checkout_id", "relative_path", "absolute_path", "uri"],
)
def test_open_document_is_bound_exactly_before_any_fact_callback(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    clean = _open_document(scope, "pkg/service.py")
    other_source = resolve_repository_source(scope, "pkg/api.py")
    source_changes = {
        "repository_id": {"repository_id": "repository:attacker"},
        "checkout_id": {"checkout_id": "checkout:attacker"},
        "relative_path": {"relative_path": "pkg/api.py"},
        "absolute_path": {"absolute_path": other_source.absolute_path},
        "uri": {"uri": other_source.uri},
    }
    malicious = replace(clean, source=replace(clean.source, **source_changes[mismatch]))
    provider_calls = 0
    structural_calls = 0

    def definition(anchor: SourceAnchor, *, deadline: float) -> ProviderLocations:
        nonlocal provider_calls
        provider_calls += 1
        return ProviderLocations((), "provider_reported", False)

    def structural(
        request: NavigationRequest, deadline: float
    ) -> tuple[NavigationLocation, ...]:
        nonlocal structural_calls
        structural_calls += 1
        return (_graph_location("pkg/api.py", 6, 15),)

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        structural=structural,
    )
    _patch_stable_attempt(monkeypatch, session, revision, malicious)
    monkeypatch.setattr(session, "definition", definition)
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
            ),
            deadline=time.monotonic() + 5,
        )
        assert result.status is NavigationStatus.ERROR
        assert result.effective_capability is None
        assert result.locations == ()
        assert result.provenance == ()
        assert provider_calls == 0
        assert structural_calls == 0
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_invalid_utf8_already_recorded_by_revision_is_error_not_stale(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = repository / "pkg/service.py"
    source_path.write_bytes(b"\xffinvalid\n")
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    provider_calls = 0

    def definition(anchor: SourceAnchor, *, deadline: float) -> ProviderLocations:
        nonlocal provider_calls
        provider_calls += 1
        return ProviderLocations((), "provider_reported", False)

    navigation, session = _navigation(repository, state_root, semantic_pyright)
    _patch_stable_attempt(monkeypatch, session, revision, document)
    monkeypatch.setattr(session, "definition", definition)
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DEFINITIONS, "pkg/service.py", 1, 0
            ),
            deadline=time.monotonic() + 5,
        )
        assert result.status is NavigationStatus.ERROR
        assert result.locations == ()
        assert provider_calls == 0
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_query_indexes_revision_entries_once_for_all_targets(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for index in range(3):
        (repository / f"pkg/target_{index}.py").write_text(
            f"value_{index} = {index}\n", encoding="utf-8"
        )
    scope = resolve_repository_scope(repository)
    base_revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    targets = tuple(
        resolve_repository_source(scope, f"pkg/target_{index}.py")
        for index in range(3)
    )

    class CountingEntries:
        def __init__(self, values) -> None:
            self.values = values
            self.iterations = 0

        def __iter__(self):
            self.iterations += 1
            return iter(self.values)

    entries = CountingEntries(base_revision.entries)
    revision = replace(base_revision, entries=entries)  # type: ignore[arg-type]
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    _patch_stable_attempt(monkeypatch, session, revision, document)
    monkeypatch.setattr(
        session,
        "definition",
        lambda anchor, *, deadline: ProviderLocations(
            tuple(
                LspLocation(
                    target.uri,
                    LspRange(LspPosition(0, 0), LspPosition(0, 7)),
                )
                for target in targets
            ),
            "provider_reported",
            False,
        ),
    )
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
            ),
            deadline=time.monotonic() + 5,
        )
        assert result.status is NavigationStatus.OK
        assert len(result.locations) == 3
        assert entries.iterations == 1
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_target_read_timeout_aborts_without_partial_publication(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    target = resolve_repository_source(scope, "pkg/api.py")
    original_read = code_navigation.read_stable_bytes
    revision_calls = 0

    def compute(_repository: RepositoryScope, *, deadline: float) -> WorkspaceRevision:
        nonlocal revision_calls
        revision_calls += 1
        return revision

    def read_target(path: Path, max_bytes: int, **kwargs) -> bytes:
        if Path(path) == target.absolute_path:
            raise TimeoutError("D:/private/target-read.py")
        return original_read(path, max_bytes, **kwargs)

    navigation, session = _navigation(repository, state_root, semantic_pyright)
    session._position_encoding = PositionEncoding.UTF8
    monkeypatch.setattr(code_navigation, "_compute_revision", compute)
    monkeypatch.setattr(code_navigation, "read_stable_bytes", read_target)
    monkeypatch.setattr(session, "synchronize", lambda value, *, deadline: None)
    monkeypatch.setattr(
        session, "open_document", lambda path, *, deadline: document
    )
    monkeypatch.setattr(
        session,
        "definition",
        lambda anchor, *, deadline: ProviderLocations(
            (
                LspLocation(
                    target.uri,
                    LspRange(LspPosition(0, 6), LspPosition(0, 15)),
                ),
            ),
            "provider_reported",
            False,
        ),
    )
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
            ),
            deadline=time.monotonic() + 5,
        )
        assert revision_calls == 1
        assert result.status is NavigationStatus.TIMEOUT
        assert result.locations == ()
        assert result.diagnostics == ()
        assert result.provenance == ()
        assert all("private" not in warning for warning in result.warnings)
    finally:
        session.close(deadline=time.monotonic() + 5)


@pytest.mark.parametrize(
    ("coverage", "partial", "expected"),
    [
        ("provider_reported", False, NavigationStatus.OK),
        ("provider_reported", True, NavigationStatus.PARTIAL),
        ("unsupported", True, NavigationStatus.UNSUPPORTED),
        ("not_ready", True, NavigationStatus.NOT_READY),
    ],
)
def test_provider_location_coverage_and_partial_are_preserved(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
    coverage: str,
    partial: bool,
    expected: NavigationStatus,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    _patch_stable_attempt(monkeypatch, session, revision, document)
    monkeypatch.setattr(
        session,
        "definition",
        lambda anchor, *, deadline: ProviderLocations((), coverage, partial),
    )
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
            ),
            deadline=time.monotonic() + 5,
        )
        assert result.status is expected
        assert result.locations == ()
        assert result.resolution is (
            ResolutionLabel.UNSUPPORTED
            if expected is NavigationStatus.UNSUPPORTED
            else ResolutionLabel.UNRESOLVED
        )
    finally:
        session.close(deadline=time.monotonic() + 5)


@pytest.mark.parametrize(
    ("coverage", "expected"),
    [
        ("unsupported", NavigationStatus.UNSUPPORTED),
        ("not_ready", NavigationStatus.NOT_READY),
    ],
)
def test_provider_call_coverage_is_not_stripped(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
    coverage: str,
    expected: NavigationStatus,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    _patch_stable_attempt(monkeypatch, session, revision, document)
    monkeypatch.setattr(
        session,
        "incoming_calls",
        lambda anchor, *, deadline: ProviderCalls("incoming", (), coverage, True),
    )
    try:
        result = navigation.query(
            NavigationRequest(
                scope,
                Capability.CALLS,
                "pkg/service.py",
                10,
                20,
                direction="incoming",
            ),
            deadline=time.monotonic() + 5,
        )
        assert result.status is expected
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_calls_fallback_classifies_only_structurally_correlated_references(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    target = resolve_repository_source(scope, "pkg/api.py")
    reference_calls = 0

    def references(anchor: SourceAnchor, *, deadline: float) -> ProviderLocations:
        nonlocal reference_calls
        reference_calls += 1
        return ProviderLocations(
            (
                LspLocation(
                    target.uri,
                    LspRange(LspPosition(0, 6), LspPosition(0, 15)),
                ),
                LspLocation(
                    target.uri,
                    LspRange(LspPosition(0, 0), LspPosition(0, 5)),
                ),
            ),
            "provider_reported",
            False,
        )

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        structural=lambda request, deadline: (
            _graph_location("pkg/api.py", 6, 15),
        ),
    )
    _patch_stable_attempt(monkeypatch, session, revision, document)
    monkeypatch.setattr(
        session,
        "incoming_calls",
        lambda anchor, *, deadline: ProviderCalls(
            "incoming", (), "unsupported", True
        ),
    )
    monkeypatch.setattr(session, "references", references)
    try:
        result = navigation.query(
            NavigationRequest(
                scope,
                Capability.CALLS,
                "pkg/service.py",
                10,
                20,
                direction="incoming",
            ),
            deadline=time.monotonic() + 5,
        )
        assert reference_calls == 1
        assert result.status is NavigationStatus.PARTIAL
        assert result.effective_capability is Capability.REFERENCES
        assert len(result.locations) == 1
        assert result.locations[0].range == PositionRange(6, 15)
        assert result.locations[0].resolution is ResolutionLabel.LSP_AND_GRAPH
        assert {item.provider for item in result.locations[0].provenance} == {
            "pyright",
            "evidence-graph",
        }
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_types_combines_type_definition_and_hover_partial(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    target = resolve_repository_source(scope, "pkg/api.py")
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    _patch_stable_attempt(monkeypatch, session, revision, document)
    monkeypatch.setattr(
        session,
        "type_definition",
        lambda anchor, *, deadline: ProviderLocations(
            (
                LspLocation(
                    target.uri,
                    LspRange(LspPosition(0, 6), LspPosition(0, 15)),
                ),
            ),
            "provider_reported",
            False,
        ),
    )
    monkeypatch.setattr(
        session,
        "hover",
        lambda anchor, *, deadline: ProviderHover("type hover", None, True),
    )
    try:
        result = navigation.query(
            NavigationRequest(scope, Capability.TYPES, "pkg/service.py", 10, 20),
            deadline=time.monotonic() + 5,
        )
        assert result.status is NavigationStatus.PARTIAL
        assert result.effective_capability is Capability.TYPES
        assert result.hover == "type hover"
        assert len(result.locations) == 1
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_types_marks_invalid_hover_range_partial_without_dropping_contents(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    _patch_stable_attempt(monkeypatch, session, revision, document)
    monkeypatch.setattr(
        session,
        "type_definition",
        lambda anchor, *, deadline: ProviderLocations(
            (), "provider_reported", False
        ),
    )
    monkeypatch.setattr(
        session,
        "hover",
        lambda anchor, *, deadline: ProviderHover(
            "type hover",
            LspRange(LspPosition(999, 0), LspPosition(999, 1)),
            False,
        ),
    )
    try:
        result = navigation.query(
            NavigationRequest(scope, Capability.TYPES, "pkg/service.py", 10, 20),
            deadline=time.monotonic() + 5,
        )
        assert result.status is NavigationStatus.PARTIAL
        assert result.hover == "type hover"
        assert "hover" in " ".join(result.warnings)
    finally:
        session.close(deadline=time.monotonic() + 5)


@pytest.mark.parametrize(
    "unavailable_part",
    ["type_definition_error", "type_definition_unsupported", "hover_error"],
)
def test_types_subrequests_are_independent(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
    unavailable_part: str,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    target = resolve_repository_source(scope, "pkg/api.py")
    calls: list[str] = []

    def type_definition(
        anchor: SourceAnchor, *, deadline: float
    ) -> ProviderLocations:
        calls.append("type_definition")
        if unavailable_part == "type_definition_error":
            raise RuntimeError("D:/private/type-definition.py")
        if unavailable_part == "type_definition_unsupported":
            return ProviderLocations((), "unsupported", True)
        return ProviderLocations(
            (
                LspLocation(
                    target.uri,
                    LspRange(LspPosition(0, 6), LspPosition(0, 15)),
                ),
            ),
            "provider_reported",
            False,
        )

    def hover(anchor: SourceAnchor, *, deadline: float) -> ProviderHover:
        calls.append("hover")
        if unavailable_part == "hover_error":
            raise RuntimeError("D:/private/hover.py")
        return ProviderHover("type hover", None, False)

    navigation, session = _navigation(repository, state_root, semantic_pyright)
    _patch_stable_attempt(monkeypatch, session, revision, document)
    monkeypatch.setattr(session, "type_definition", type_definition)
    monkeypatch.setattr(session, "hover", hover)
    try:
        result = navigation.query(
            NavigationRequest(scope, Capability.TYPES, "pkg/service.py", 10, 20),
            deadline=time.monotonic() + 5,
        )
        assert calls == ["type_definition", "hover"]
        assert result.status is NavigationStatus.PARTIAL
        assert result.effective_capability is Capability.TYPES
        if unavailable_part == "hover_error":
            assert len(result.locations) == 1
            assert result.hover is None
        else:
            assert result.locations == ()
            assert result.hover == "type hover"
        assert all("private" not in warning for warning in result.warnings)
    finally:
        session.close(deadline=time.monotonic() + 5)


@pytest.mark.parametrize(
    ("failure", "expected", "expected_callbacks"),
    [
        (TimeoutError("late"), NavigationStatus.TIMEOUT, 0),
        (RuntimeError("D:/private/secret.py"), NavigationStatus.PARTIAL, 1),
    ],
)
def test_provider_failure_still_uses_deadline_aware_structural_fallback(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    expected: NavigationStatus,
    expected_callbacks: int,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    seen_deadlines: list[float] = []

    def structural(
        request: NavigationRequest, deadline: float
    ) -> tuple[NavigationLocation, ...]:
        seen_deadlines.append(deadline)
        return (_graph_location("pkg/api.py", 6, 15),)

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        structural=structural,
    )
    _patch_stable_attempt(monkeypatch, session, revision, document)

    def fail(_anchor, *, deadline: float) -> ProviderLocations:
        raise failure

    monkeypatch.setattr(session, "definition", fail)
    absolute_deadline = time.monotonic() + 5
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
            ),
            deadline=absolute_deadline,
        )
        assert result.status is expected
        assert len(seen_deadlines) == expected_callbacks
        if expected_callbacks == 0:
            assert result.locations == ()
        else:
            assert seen_deadlines == [absolute_deadline]
            assert len(result.locations) == 1
            assert result.locations[0].resolution is ResolutionLabel.GRAPH_CANDIDATE
        assert all("private" not in warning and "secret" not in warning for warning in result.warnings)
    finally:
        session.close(deadline=time.monotonic() + 5)


@pytest.mark.parametrize("stage", ["synchronize", "open_document"])
def test_provider_setup_failure_publishes_only_fresh_structural_fallback(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    callbacks = 0

    def structural(
        request: NavigationRequest, deadline: float
    ) -> tuple[NavigationLocation, ...]:
        nonlocal callbacks
        callbacks += 1
        return (_graph_location("pkg/api.py", 6, 15),)

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        structural=structural,
    )
    _patch_stable_attempt(monkeypatch, session, revision, document)
    session._readiness = "query_ready"

    def fail(*args, **kwargs):
        raise RuntimeError("D:/private/provider-sensitive.py")

    monkeypatch.setattr(session, stage, fail)
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
            ),
            deadline=time.monotonic() + 5,
        )
        assert callbacks == 1
        assert result.status is NavigationStatus.PARTIAL
        assert result.effective_capability is None
        assert result.readiness == "query_ready"
        assert len(result.locations) == 1
        assert result.locations[0].resolution is ResolutionLabel.GRAPH_CANDIDATE
        assert {item.provider for item in result.provenance} == {"evidence-graph"}
        assert "setup" in " ".join(result.warnings)
        assert all("private" not in warning for warning in result.warnings)
    finally:
        session.close(deadline=time.monotonic() + 5)


@pytest.mark.parametrize("stage", ["synchronize", "open_document"])
@pytest.mark.parametrize(
    ("request_path", "request_line"),
    [("pkg/missing.py", 1), ("pkg/service.py", 1_000)],
)
def test_setup_failure_never_bypasses_request_source_or_anchor_validation(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    request_path: str,
    request_line: int,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    structural_calls = 0

    def structural(
        request: NavigationRequest, deadline: float
    ) -> tuple[NavigationLocation, ...]:
        nonlocal structural_calls
        structural_calls += 1
        return (_graph_location("pkg/api.py", 6, 15),)

    def fail_setup(*args, **kwargs):
        raise RuntimeError("provider setup failed")

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        structural=structural,
    )
    monkeypatch.setattr(
        code_navigation,
        "_compute_revision",
        lambda repository, *, deadline: revision,
    )
    monkeypatch.setattr(session, "synchronize", lambda value, *, deadline: None)
    monkeypatch.setattr(
        session,
        "open_document",
        lambda path, *, deadline: _open_document(scope, "pkg/service.py"),
    )
    monkeypatch.setattr(session, stage, fail_setup)
    try:
        result = navigation.query(
            NavigationRequest(
                scope,
                Capability.DEFINITIONS,
                request_path,
                request_line,
                0,
            ),
            deadline=time.monotonic() + 5,
        )

        assert result.status is NavigationStatus.ERROR
        assert result.locations == ()
        assert result.provenance == ()
        assert structural_calls == 0
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_setup_failure_retries_request_source_disk_mismatch_before_fallback(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    (repository / "pkg/service.py").write_bytes(b"changed_after_revision = True\n")
    revision_calls = 0
    structural_calls = 0

    def compute(
        repository_scope: RepositoryScope, *, deadline: float
    ) -> WorkspaceRevision:
        nonlocal revision_calls
        revision_calls += 1
        return revision

    def structural(
        request: NavigationRequest, deadline: float
    ) -> tuple[NavigationLocation, ...]:
        nonlocal structural_calls
        structural_calls += 1
        return (_graph_location("pkg/api.py", 6, 15),)

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        structural=structural,
    )
    monkeypatch.setattr(code_navigation, "_compute_revision", compute)
    monkeypatch.setattr(
        session,
        "synchronize",
        lambda value, *, deadline: (_ for _ in ()).throw(RuntimeError("not ready")),
    )
    try:
        result = navigation.query(
            NavigationRequest(
                scope,
                Capability.DEFINITIONS,
                "pkg/service.py",
                1,
                0,
            ),
            deadline=time.monotonic() + 5,
        )

        assert result.status is NavigationStatus.STALE
        assert result.locations == ()
        assert result.provenance == ()
        assert revision_calls == 2
        assert structural_calls == 0
    finally:
        session.close(deadline=time.monotonic() + 5)


@pytest.mark.parametrize("mutate_every_attempt", [False, True])
def test_setup_failure_validates_after_synchronize_before_aba_fallback(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
    mutate_every_attempt: bool,
) -> None:
    scope = resolve_repository_scope(repository)
    source_path = repository / "pkg/service.py"
    content_a = source_path.read_bytes()
    content_b = b"changed_by_synchronize = True\n"
    revision_a = _revision(repository, "a")
    source_path.write_bytes(content_b)
    revision_b = _revision(repository, "b")
    source_path.write_bytes(content_a)
    revisions = {content_a: revision_a, content_b: revision_b}
    synchronize_calls = 0
    structural_calls = 0
    events: list[str] = []

    def compute(
        repository_scope: RepositoryScope, *, deadline: float
    ) -> WorkspaceRevision:
        return revisions[source_path.read_bytes()]

    def synchronize(value: WorkspaceRevision, *, deadline: float) -> None:
        nonlocal synchronize_calls
        synchronize_calls += 1
        events.append(f"synchronize:{synchronize_calls}")
        if synchronize_calls == 1:
            assert value is revision_a
            source_path.write_bytes(content_b)
        else:
            assert value is revision_b
            if mutate_every_attempt:
                source_path.write_bytes(content_a)
        raise RuntimeError("provider setup failed after mutation")

    def structural(
        request: NavigationRequest, deadline: float
    ) -> tuple[NavigationLocation, ...]:
        nonlocal structural_calls
        structural_calls += 1
        events.append("structural")
        if synchronize_calls == 1:
            source_path.write_bytes(content_a)
        return (_graph_location("pkg/api.py", 6, 15),)

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        structural=structural,
    )
    monkeypatch.setattr(code_navigation, "_compute_revision", compute)
    monkeypatch.setattr(session, "synchronize", synchronize)
    try:
        result = navigation.query(
            NavigationRequest(
                scope,
                Capability.DEFINITIONS,
                "pkg/service.py",
                1,
                0,
            ),
            deadline=time.monotonic() + 5,
        )

        assert synchronize_calls == 2
        if mutate_every_attempt:
            assert events == ["synchronize:1", "synchronize:2"]
            assert structural_calls == 0
            assert result.status is NavigationStatus.STALE
            assert result.locations == ()
            assert result.provenance == ()
        else:
            assert events == ["synchronize:1", "synchronize:2", "structural"]
            assert structural_calls == 1
            assert result.status is NavigationStatus.PARTIAL
            assert result.workspace_revision_before == revision_b.revision_sha256
            assert result.workspace_revision_after == revision_b.revision_sha256
            assert len(result.locations) == 1
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_query_retries_when_request_source_first_appears_inside_revision_fence(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    relative_path = "pkg/appeared.py"
    target = repository / relative_path
    content = b"appeared = True\n"
    source = resolve_repository_source(scope, relative_path, must_exist=False)
    document = OpenDocument(
        source,
        content,
        hashlib.sha256(content).hexdigest(),
        1,
    )
    original_resolve = code_navigation.resolve_repository_source
    provider_calls = 0

    def resolve(repository_scope: RepositoryScope, path: str):
        if path == relative_path and not target.exists():
            target.write_bytes(content)
        return original_resolve(repository_scope, path)

    def definition(anchor: SourceAnchor, *, deadline: float) -> ProviderLocations:
        nonlocal provider_calls
        provider_calls += 1
        return ProviderLocations((), "provider_reported", False)

    navigation, session = _navigation(repository, state_root, semantic_pyright)
    session._position_encoding = PositionEncoding.UTF8
    monkeypatch.setattr(code_navigation, "resolve_repository_source", resolve)
    monkeypatch.setattr(session, "synchronize", lambda value, *, deadline: None)
    monkeypatch.setattr(
        session,
        "open_document",
        lambda path, *, deadline: document,
    )
    monkeypatch.setattr(session, "definition", definition)
    try:
        result = navigation.query(
            NavigationRequest(
                scope,
                Capability.DEFINITIONS,
                relative_path,
                1,
                0,
            ),
            deadline=time.monotonic() + 5,
        )

        assert result.status is NavigationStatus.OK
        assert result.workspace_revision_before == result.workspace_revision_after
        assert provider_calls == 1
    finally:
        session.close(deadline=time.monotonic() + 5)


@pytest.mark.parametrize("stage", ["synchronize", "open_document"])
@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (RuntimeError("D:/private/provider-not-ready.py"), NavigationStatus.NOT_READY),
        (TimeoutError("D:/private/provider-timeout.py"), NavigationStatus.NOT_READY),
    ],
)
def test_provider_setup_distinguishes_not_ready_from_timeout(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    failure: Exception,
    expected: NavigationStatus,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    _patch_stable_attempt(monkeypatch, session, revision, document)
    session._readiness = "query_ready"

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(session, stage, fail)
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
            ),
            deadline=time.monotonic() + 5,
        )
        assert result.status is expected
        assert result.effective_capability is None
        assert result.readiness == "query_ready"
        assert result.locations == ()
        assert all("private" not in warning for warning in result.warnings)
    finally:
        session.close(deadline=time.monotonic() + 5)


@pytest.mark.parametrize("useful_graph", [False, True])
@pytest.mark.parametrize("mutate_every_attempt", [False, True])
def test_setup_failure_fallback_retries_freshness_before_publication(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
    useful_graph: bool,
    mutate_every_attempt: bool,
) -> None:
    scope = resolve_repository_scope(repository)
    revisions = (
        _revision(repository, "a"),
        _revision(repository, "b"),
        _revision(repository, "c"),
    )
    current_revision = 0
    callbacks = 0

    def compute(
        repository: RepositoryScope, *, deadline: float
    ) -> WorkspaceRevision:
        return revisions[current_revision]

    def structural(
        request: NavigationRequest, deadline: float
    ) -> tuple[NavigationLocation, ...]:
        nonlocal callbacks, current_revision
        callbacks += 1
        if callbacks == 1 or mutate_every_attempt:
            current_revision += 1
        return (
            (_graph_location("pkg/api.py", 6, 15),)
            if useful_graph
            else ()
        )

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        structural=structural,
    )
    session._readiness = "not_ready"
    monkeypatch.setattr(code_navigation, "_compute_revision", compute)
    monkeypatch.setattr(
        session,
        "synchronize",
        lambda value, *, deadline: (_ for _ in ()).throw(RuntimeError("not ready")),
    )
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
            ),
            deadline=time.monotonic() + 5,
        )
        assert callbacks == 2
        assert result.effective_capability is None
        if mutate_every_attempt:
            assert result.status is NavigationStatus.STALE
            assert result.locations == ()
            assert result.provenance == ()
        elif useful_graph:
            assert result.status is NavigationStatus.PARTIAL
            assert len(result.locations) == 1
        else:
            assert result.status is NavigationStatus.NOT_READY
            assert result.locations == ()
    finally:
        session.close(deadline=time.monotonic() + 5)


@pytest.mark.parametrize("stage", ["synchronize", "open_document"])
def test_setup_timeout_after_absolute_deadline_skips_structural_fallback(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    clock = {"now": 1.0}
    real_monotonic = time.monotonic
    callbacks = 0

    def expire(*args, **kwargs):
        clock["now"] = 11.0
        raise TimeoutError("setup timeout")

    def structural(*args):
        nonlocal callbacks
        callbacks += 1
        return (_graph_location("pkg/api.py", 6, 15),)

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        structural=structural,
    )
    _patch_stable_attempt(monkeypatch, session, revision, document)
    monkeypatch.setattr(session, stage, expire)
    monkeypatch.setattr(code_navigation.time, "monotonic", lambda: clock["now"])
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
            ),
            deadline=10.0,
        )
        assert result.status is NavigationStatus.TIMEOUT
        assert result.effective_capability is None
        assert result.locations == ()
        assert callbacks == 0
    finally:
        monkeypatch.setattr(code_navigation.time, "monotonic", real_monotonic)
        session.close(deadline=real_monotonic() + 5)


@pytest.mark.parametrize(
    "early_stage",
    ["unsupported", "expired", "revision_timeout", "revision_error", "empty_revision"],
)
def test_early_results_preserve_live_session_readiness(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
    early_stage: str,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    session._readiness = "query_ready"
    if early_stage == "revision_timeout":
        monkeypatch.setattr(
            code_navigation,
            "_compute_revision",
            lambda repository, *, deadline: (_ for _ in ()).throw(TimeoutError()),
        )
    elif early_stage == "revision_error":
        monkeypatch.setattr(
            code_navigation,
            "_compute_revision",
            lambda repository, *, deadline: (_ for _ in ()).throw(RuntimeError()),
        )
    elif early_stage == "empty_revision":
        monkeypatch.setattr(
            code_navigation,
            "_compute_revision",
            lambda repository, *, deadline: replace(revision, revision_sha256=""),
        )
    capability = (
        Capability.DECLARATIONS
        if early_stage == "unsupported"
        else Capability.DEFINITIONS
    )
    deadline = 0.0 if early_stage == "expired" else time.monotonic() + 5
    try:
        result = navigation.query(
            NavigationRequest(scope, capability, "pkg/service.py", 10, 20),
            deadline=deadline,
        )
        assert result.readiness == "query_ready"
        assert result.effective_capability is None
        assert result.locations == ()
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_setup_failure_structural_interruption_still_propagates(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")

    def interrupted(*args):
        raise code_navigation.NavigationInterruption

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        structural=interrupted,
    )
    monkeypatch.setattr(
        code_navigation,
        "_compute_revision",
        lambda repository, *, deadline: revision,
    )
    monkeypatch.setattr(
        session,
        "synchronize",
        lambda value, *, deadline: (_ for _ in ()).throw(RuntimeError("not ready")),
    )
    try:
        with pytest.raises(code_navigation.NavigationInterruption):
            navigation.query(
                NavigationRequest(
                    scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
                ),
                deadline=time.monotonic() + 5,
            )
    finally:
        session.close(deadline=time.monotonic() + 5)


@pytest.mark.parametrize(
    ("stage", "expected"),
    [
        ("source_validation", NavigationStatus.ERROR),
        ("anchor_validation", NavigationStatus.ERROR),
        ("post_revision_timeout", NavigationStatus.TIMEOUT),
        ("post_revision_error", NavigationStatus.ERROR),
        ("post_revision_stale", NavigationStatus.STALE),
    ],
)
def test_post_setup_empty_results_preserve_readiness_and_empty_contract(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    expected: NavigationStatus,
) -> None:
    scope = resolve_repository_scope(repository)
    revisions = tuple(_revision(repository, marker) for marker in "abcd")
    document = _open_document(scope, "pkg/service.py")
    if stage == "source_validation":
        document = replace(document, source_sha256="0" * 64)
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    session._position_encoding = PositionEncoding.UTF8
    session._readiness = "query_ready"
    monkeypatch.setattr(session, "synchronize", lambda value, *, deadline: None)
    monkeypatch.setattr(
        session,
        "open_document",
        lambda path, *, deadline: document,
    )
    monkeypatch.setattr(
        session,
        "definition",
        lambda anchor, *, deadline: ProviderLocations(
            (), "provider_reported", False
        ),
    )
    revision_calls = 0

    def compute(
        repository: RepositoryScope, *, deadline: float
    ) -> WorkspaceRevision:
        nonlocal revision_calls
        revision_calls += 1
        if stage == "post_revision_timeout" and revision_calls == 2:
            raise TimeoutError
        if stage == "post_revision_error" and revision_calls == 2:
            raise RuntimeError
        if stage == "post_revision_stale":
            return revisions[revision_calls - 1]
        return revisions[0]

    monkeypatch.setattr(code_navigation, "_compute_revision", compute)
    request_line = 1000 if stage == "anchor_validation" else 10
    try:
        result = navigation.query(
            NavigationRequest(
                scope,
                Capability.DEFINITIONS,
                "pkg/service.py",
                request_line,
                20,
            ),
            deadline=time.monotonic() + 5,
        )
        assert result.status is expected
        assert result.readiness == "query_ready"
        assert result.effective_capability is None
        assert result.total == 0
        assert result.symbol is None
        assert result.hover is None
        assert result.locations == ()
        assert result.diagnostics == ()
        assert result.provenance == ()
        assert isinstance(result.locations, tuple)
        assert isinstance(result.diagnostics, tuple)
        assert isinstance(result.provenance, tuple)
        assert isinstance(result.warnings, tuple)
    finally:
        session.close(deadline=time.monotonic() + 5)


@pytest.mark.parametrize(
    ("callback_kind", "expected"),
    [
        ("structural", NavigationStatus.PARTIAL),
        ("resolver", NavigationStatus.ERROR),
        ("edge", NavigationStatus.ERROR),
    ],
)
def test_callback_exceptions_are_redacted_at_every_boundary(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
    callback_kind: str,
    expected: NavigationStatus,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")

    def fail(*args, **kwargs):
        raise KeyError("D:/private/callback-secret.py")

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        structural=fail if callback_kind == "structural" else None,
        resolver=fail if callback_kind == "resolver" else None,
        edge=fail if callback_kind == "edge" else None,
    )
    monkeypatch.setattr(
        code_navigation,
        "_compute_revision",
        lambda repository, *, deadline: revision,
    )
    try:
        if callback_kind == "structural":
            document = _open_document(scope, "pkg/service.py")
            session._position_encoding = PositionEncoding.UTF8
            monkeypatch.setattr(
                session, "synchronize", lambda value, *, deadline: None
            )
            monkeypatch.setattr(
                session, "open_document", lambda path, *, deadline: document
            )
            monkeypatch.setattr(
                session,
                "definition",
                lambda anchor, *, deadline: ProviderLocations(
                    (), "provider_reported", False
                ),
            )
            result = navigation.query(
                NavigationRequest(
                    scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
                ),
                deadline=time.monotonic() + 5,
            )
        elif callback_kind == "resolver":
            result = navigation.resolve_symbol(
                "PublicApi",
                repository=scope,
                deadline=time.monotonic() + 5,
            )
        else:
            result = navigation.verify_edge(
                _source_anchor(scope, "pkg/service.py", 10, 15),
                _source_anchor(scope, "pkg/api.py", 2, 8),
                repository=scope,
                deadline=time.monotonic() + 5,
            )
        assert result.status is expected
        assert result.locations == ()
        assert all(
            "private" not in warning and "secret" not in warning
            for warning in result.warnings
        )
    finally:
        session.close(deadline=time.monotonic() + 5)


@pytest.mark.parametrize("callback_kind", ["structural", "resolver", "edge"])
def test_navigation_interruption_propagates_from_every_callback_boundary(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
    callback_kind: str,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")

    def interrupted(*args, **kwargs):
        raise code_navigation.NavigationInterruption

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        structural=interrupted if callback_kind == "structural" else None,
        resolver=interrupted if callback_kind == "resolver" else None,
        edge=interrupted if callback_kind == "edge" else None,
    )
    monkeypatch.setattr(
        code_navigation,
        "_compute_revision",
        lambda repository, *, deadline: revision,
    )
    try:
        with pytest.raises(code_navigation.NavigationInterruption):
            if callback_kind == "structural":
                document = _open_document(scope, "pkg/service.py")
                session._position_encoding = PositionEncoding.UTF8
                monkeypatch.setattr(
                    session, "synchronize", lambda value, *, deadline: None
                )
                monkeypatch.setattr(
                    session, "open_document", lambda path, *, deadline: document
                )
                monkeypatch.setattr(
                    session,
                    "definition",
                    lambda anchor, *, deadline: ProviderLocations(
                        (), "provider_reported", False
                    ),
                )
                navigation.query(
                    NavigationRequest(
                        scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
                    ),
                    deadline=time.monotonic() + 5,
                )
            elif callback_kind == "resolver":
                navigation.resolve_symbol(
                    "PublicApi",
                    repository=scope,
                    deadline=time.monotonic() + 5,
                )
            else:
                navigation.verify_edge(
                    _source_anchor(scope, "pkg/service.py", 10, 15),
                    _source_anchor(scope, "pkg/api.py", 2, 8),
                    repository=scope,
                    deadline=time.monotonic() + 5,
                )
    finally:
        session.close(deadline=time.monotonic() + 5)


@pytest.mark.parametrize("capability", [Capability.DECLARATIONS, Capability.IMPORTS, Capability.INHERITANCE])
def test_unrouted_capabilities_do_not_invoke_provider_or_structural_fallback(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
    capability: Capability,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    calls: list[str] = []
    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        structural=lambda request, deadline: (
            calls.append("structural") or _graph_location("pkg/api.py", 6, 15),
        ),
    )
    monkeypatch.setattr(
        code_navigation,
        "_compute_revision",
        lambda repository, *, deadline: calls.append("revision") or revision,
    )
    monkeypatch.setattr(
        session,
        "synchronize",
        lambda value, *, deadline: calls.append("synchronize"),
    )
    monkeypatch.setattr(
        session,
        "open_document",
        lambda path, *, deadline: calls.append("open") or document,
    )
    try:
        result = navigation.query(
            NavigationRequest(scope, capability, "pkg/service.py", 10, 20),
            deadline=time.monotonic() + 5,
        )
        assert result.status is NavigationStatus.UNSUPPORTED
        assert result.effective_capability is None
        assert result.resolution is ResolutionLabel.UNSUPPORTED
        assert result.locations == ()
        assert calls == []
    finally:
        session.close(deadline=time.monotonic() + 5)


@pytest.mark.parametrize(
    ("encoding", "start_character", "end_character"),
    [
        (PositionEncoding.UTF8, 14, 16),
        (PositionEncoding.UTF16, 12, 13),
        (PositionEncoding.UTF32, 11, 12),
    ],
)
def test_cross_file_unicode_targets_use_negotiated_position_encoding(
    repository: Path,
    state_root: Path,
    encoding: PositionEncoding,
    start_character: int,
    end_character: int,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={
            "capabilities": {
                "definitionProvider": True,
                "documentSymbolProvider": True,
                "positionEncoding": encoding.value,
                "textDocumentSync": 2,
            },
            "responses": {
                "textDocument/definition": [
                    {
                        "uri": "$UNICODE_URI",
                        "range": {
                            "start": {"line": 0, "character": start_character},
                            "end": {"line": 0, "character": end_character},
                        },
                    }
                ]
            },
        },
    )
    navigation, session = _navigation(repository, state_root, fixture)
    try:
        result = navigation.query(
            NavigationRequest(
                resolve_repository_scope(repository),
                Capability.DEFINITIONS,
                "pkg/service.py",
                10,
                20,
            ),
            deadline=time.monotonic() + 20,
        )
        assert result.status is NavigationStatus.OK
        assert result.position_encoding is encoding
        assert len(result.locations) == 1
        location = result.locations[0]
        assert location.path == "pkg/unicode_api.py"
        assert location.range == PositionRange(14, 16)
        assert location.line == 1
        assert location.character == 14
    finally:
        navigation.close(deadline=time.monotonic() + 5)


def test_provider_target_with_stale_pre_revision_hash_returns_stale(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    clean_revision = _revision(repository, "a")
    revision = replace(
        clean_revision,
        entries=tuple(
            replace(entry, sha256="0" * 64)
            if entry.path == "pkg/api.py"
            else entry
            for entry in clean_revision.entries
        ),
    )
    document = _open_document(scope, "pkg/service.py")
    target = resolve_repository_source(scope, "pkg/api.py")
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    _patch_stable_attempt(monkeypatch, session, revision, document)
    monkeypatch.setattr(
        session,
        "definition",
        lambda anchor, *, deadline: ProviderLocations(
            (
                LspLocation(
                    target.uri,
                    LspRange(LspPosition(0, 6), LspPosition(0, 15)),
                ),
            ),
            "provider_reported",
            False,
        ),
    )
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
            ),
            deadline=time.monotonic() + 5,
        )
        assert result.status is NavigationStatus.STALE
        assert result.locations == ()
        assert "changed" in " ".join(result.warnings)
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_provider_uri_normalization_is_attempt_local_for_repeated_locations(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    target = resolve_repository_source(scope, "pkg/api.py")
    external_uri = (repository.parent / "external-sensitive.py").resolve().as_uri()
    ranges = (
        LspRange(LspPosition(0, 0), LspPosition(0, 5)),
        LspRange(LspPosition(0, 6), LspPosition(0, 15)),
        LspRange(LspPosition(1, 4), LspPosition(1, 7)),
        LspRange(LspPosition(1, 8), LspPosition(1, 14)),
        LspRange(LspPosition(2, 8), LspPosition(2, 14)),
    )
    provider_locations = tuple(
        LspLocation(target.uri, range_) for range_ in ranges
    ) + (
        LspLocation(
            external_uri,
            LspRange(LspPosition(0, 0), LspPosition(0, 1)),
        ),
        LspLocation(
            external_uri,
            LspRange(LspPosition(0, 1), LspPosition(0, 2)),
        ),
    )
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    _patch_stable_attempt(monkeypatch, session, revision, document)
    monkeypatch.setattr(
        session,
        "definition",
        lambda anchor, *, deadline: ProviderLocations(
            provider_locations, "provider_reported", False
        ),
    )
    real_normalize_provider_uri = code_navigation.normalize_provider_uri
    normalized_uris: list[str] = []

    def recording_normalize_provider_uri(
        repository_scope: RepositoryScope,
        uri: str,
    ):
        normalized_uris.append(uri)
        return real_normalize_provider_uri(repository_scope, uri)

    monkeypatch.setattr(
        code_navigation,
        "normalize_provider_uri",
        recording_normalize_provider_uri,
    )
    request = NavigationRequest(
        scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
    )
    try:
        results = tuple(
            navigation.query(request, deadline=time.monotonic() + 5)
            for _ in range(2)
        )

        for result in results:
            assert result.status is NavigationStatus.PARTIAL
            assert len(result.locations) == 5
            assert all(location.path == "pkg/api.py" for location in result.locations)
            assert len({location.range for location in result.locations}) == 5
        assert normalized_uris == [
            target.uri,
            external_uri,
            target.uri,
            external_uri,
        ]
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_external_provider_target_is_filtered_without_path_disclosure(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    external_uri = (repository.parent / "external-sensitive.py").resolve().as_uri()
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    _patch_stable_attempt(monkeypatch, session, revision, document)
    monkeypatch.setattr(
        session,
        "definition",
        lambda anchor, *, deadline: ProviderLocations(
            (
                LspLocation(
                    external_uri,
                    LspRange(LspPosition(0, 0), LspPosition(0, 1)),
                ),
            ),
            "provider_reported",
            False,
        ),
    )
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
            ),
            deadline=time.monotonic() + 5,
        )
        assert result.status is NavigationStatus.PARTIAL
        assert result.locations == ()
        assert all("external-sensitive" not in warning for warning in result.warnings)
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_structural_targets_are_revision_validated_and_rebuilt(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")

    def structural(
        request: NavigationRequest, deadline: float
    ) -> tuple[NavigationLocation, ...]:
        return (
            _graph_location("pkg/api.py", 6, 15),
            _graph_location("pkg/missing.py", 0, 1),
            _graph_location("pkg/api.py", 0, 100_000),
        )

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        structural=structural,
    )
    _patch_stable_attempt(monkeypatch, session, revision, document)
    monkeypatch.setattr(
        session,
        "definition",
        lambda anchor, *, deadline: ProviderLocations(
            (), "provider_reported", False
        ),
    )
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
            ),
            deadline=time.monotonic() + 5,
        )
        assert result.status is NavigationStatus.PARTIAL
        assert len(result.locations) == 1
        assert result.locations[0].path == "pkg/api.py"
        assert result.locations[0].line == 1
        assert result.locations[0].character == 6
        assert result.locations[0].resolution is ResolutionLabel.GRAPH_CANDIDATE
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_matching_graph_fact_upgrades_lsp_and_graph_only_fact_appends_after_lsp(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    target = resolve_repository_source(scope, "pkg/api.py")

    def structural(
        request: NavigationRequest, deadline: float
    ) -> tuple[NavigationLocation, ...]:
        return (
            _graph_location("pkg/api.py", 6, 15),
            _graph_location("pkg/api.py", 0, 5),
        )

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        structural=structural,
    )
    _patch_stable_attempt(monkeypatch, session, revision, document)
    monkeypatch.setattr(
        session,
        "definition",
        lambda anchor, *, deadline: ProviderLocations(
            (
                LspLocation(
                    target.uri,
                    LspRange(LspPosition(0, 6), LspPosition(0, 15)),
                ),
            ),
            "provider_reported",
            False,
        ),
    )
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
            ),
            deadline=time.monotonic() + 5,
        )
        assert len(result.locations) == 2
        confirmed, candidate = result.locations
        assert confirmed.range == PositionRange(6, 15)
        assert confirmed.resolution is ResolutionLabel.LSP_AND_GRAPH
        assert {item.provider for item in confirmed.provenance} == {
            "pyright",
            "evidence-graph",
        }
        assert candidate.range == PositionRange(0, 5)
        assert candidate.resolution is ResolutionLabel.GRAPH_CANDIDATE
        assert {item.provider for item in result.provenance} == {
            "pyright",
            "evidence-graph",
        }
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_structural_fact_count_is_capped_at_ten_thousand(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    many_path = repository / "pkg/many.py"
    many_path.write_bytes(b"x" * 10_001 + b"\n")
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")

    def structural(
        request: NavigationRequest, deadline: float
    ) -> tuple[NavigationLocation, ...]:
        return tuple(
            _graph_location("pkg/many.py", offset, offset)
            for offset in range(10_001)
        )

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        structural=structural,
    )
    _patch_stable_attempt(monkeypatch, session, revision, document)
    monkeypatch.setattr(
        session,
        "definition",
        lambda anchor, *, deadline: ProviderLocations(
            (), "provider_reported", False
        ),
    )
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
            ),
            deadline=time.monotonic() + 20,
        )
        assert result.status is NavigationStatus.PARTIAL
        assert result.total == 10_000
        assert len(result.locations) == 10_000
        assert "limit" in " ".join(result.warnings)
    finally:
        session.close(deadline=time.monotonic() + 5)


@pytest.mark.parametrize("operation", ["query", "resolve_symbol"])
def test_structural_cap_is_applied_after_unique_deduplication(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    candidates = (
        _graph_location("pkg/api.py", 0, 5),
        _graph_location("pkg/api.py", 0, 5),
        _graph_location("pkg/api.py", 6, 15),
    )
    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        structural=(
            (lambda request, deadline: candidates) if operation == "query" else None
        ),
        resolver=(
            (lambda symbol, repository, deadline: candidates)
            if operation == "resolve_symbol"
            else None
        ),
    )
    monkeypatch.setattr(code_navigation, "_MAX_NAVIGATION_FACTS", 2)
    monkeypatch.setattr(
        code_navigation,
        "_compute_revision",
        lambda repository, *, deadline: revision,
    )
    try:
        if operation == "query":
            document = _open_document(scope, "pkg/service.py")
            session._position_encoding = PositionEncoding.UTF8
            monkeypatch.setattr(
                session, "synchronize", lambda value, *, deadline: None
            )
            monkeypatch.setattr(
                session, "open_document", lambda path, *, deadline: document
            )
            monkeypatch.setattr(
                session,
                "definition",
                lambda anchor, *, deadline: ProviderLocations(
                    (), "provider_reported", False
                ),
            )
            result = navigation.query(
                NavigationRequest(
                    scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
                ),
                deadline=time.monotonic() + 5,
            )
        else:
            result = navigation.resolve_symbol(
                "PublicApi",
                repository=scope,
                deadline=time.monotonic() + 5,
            )
        assert result.status is NavigationStatus.PARTIAL
        assert result.total == 2
        assert [location.range for location in result.locations] == [
            PositionRange(0, 5),
            PositionRange(6, 15),
        ]
        assert "limit" not in " ".join(result.warnings)
    finally:
        session.close(deadline=time.monotonic() + 5)


@pytest.mark.parametrize("operation", ["query", "resolve_symbol"])
def test_structural_callback_input_is_bounded_before_fact_deduplication(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")

    def candidates(*args):
        for _ in range(4):
            yield _graph_location("pkg/api.py", 0, 5)
        raise AssertionError("callback iterator consumed past its input bound")

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        structural=candidates if operation == "query" else None,
        resolver=candidates if operation == "resolve_symbol" else None,
    )
    monkeypatch.setattr(
        code_navigation, "_MAX_NAVIGATION_INPUT_VALUES", 3, raising=False
    )
    monkeypatch.setattr(
        code_navigation,
        "_compute_revision",
        lambda repository, *, deadline: revision,
    )
    try:
        if operation == "query":
            document = _open_document(scope, "pkg/service.py")
            session._position_encoding = PositionEncoding.UTF8
            monkeypatch.setattr(
                session, "synchronize", lambda value, *, deadline: None
            )
            monkeypatch.setattr(
                session, "open_document", lambda path, *, deadline: document
            )
            monkeypatch.setattr(
                session,
                "definition",
                lambda anchor, *, deadline: ProviderLocations(
                    (), "provider_reported", False
                ),
            )
            result = navigation.query(
                NavigationRequest(
                    scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
                ),
                deadline=time.monotonic() + 5,
            )
        else:
            result = navigation.resolve_symbol(
                "PublicApi",
                repository=scope,
                deadline=None,
            )
        assert result.status is NavigationStatus.PARTIAL
        assert result.total == 1
        assert "input bound" in " ".join(result.warnings)
        assert "failed" not in " ".join(result.warnings)
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_diagnostics_preserve_severity_code_message_version_related_and_partial(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py", version=7)
    api = resolve_repository_source(scope, "pkg/api.py")
    external_uri = (repository.parent / "diagnostic-secret.py").resolve().as_uri()
    diagnostic_range = LspRange(LspPosition(9, 15), LspPosition(9, 25))
    related_range = LspRange(LspPosition(1, 8), LspPosition(1, 14))
    valid = LspDiagnostic(
        document.source.uri,
        diagnostic_range,
        2,
        "reportUnknownMemberType",
        "Member type is unknown",
        (
            (LspLocation(api.uri, related_range), "Declared here"),
            (
                LspLocation(
                    external_uri,
                    LspRange(LspPosition(0, 0), LspPosition(0, 1)),
                ),
                "External",
            ),
        ),
    )
    missing_severity = LspDiagnostic(
        document.source.uri,
        diagnostic_range,
        None,
        "missing",
        "Missing severity",
        (),
    )
    invalid_severity = LspDiagnostic(
        document.source.uri,
        diagnostic_range,
        5,
        "invalid",
        "Invalid severity",
        (),
    )
    boolean_severity = LspDiagnostic(
        document.source.uri,
        diagnostic_range,
        True,
        "boolean",
        "Boolean severity",
        (),
    )
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    _patch_stable_attempt(monkeypatch, session, revision, document)
    monkeypatch.setattr(
        session,
        "diagnostics",
        lambda path, *, deadline: ProviderDiagnostics(
            (valid, missing_severity, invalid_severity, boolean_severity, valid),
            7,
            True,
        ),
    )
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DIAGNOSTICS, "pkg/service.py", 10, 20
            ),
            deadline=time.monotonic() + 5,
        )
        assert result.status is NavigationStatus.PARTIAL
        assert result.document_version == 7
        assert result.total == 1
        assert result.locations == ()
        assert len(result.diagnostics) == 1
        diagnostic = result.diagnostics[0]
        assert diagnostic.path == "pkg/service.py"
        assert diagnostic.severity is DiagnosticSeverity.WARNING
        assert diagnostic.code == "reportUnknownMemberType"
        assert diagnostic.message == "Member type is unknown"
        assert diagnostic.range == PositionRange(216, 226)
        assert len(diagnostic.related) == 1
        assert diagnostic.related[0].path == "pkg/api.py"
        assert diagnostic.related[0].range == PositionRange(25, 31)
        assert all("diagnostic-secret" not in warning for warning in result.warnings)
    finally:
        session.close(deadline=time.monotonic() + 5)


@pytest.mark.parametrize(
    ("severity", "expected"),
    [
        (1, DiagnosticSeverity.ERROR),
        (2, DiagnosticSeverity.WARNING),
        (3, DiagnosticSeverity.INFORMATION),
        (4, DiagnosticSeverity.HINT),
    ],
)
def test_lsp_diagnostic_severity_mapping_is_exact(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
    severity: int,
    expected: DiagnosticSeverity,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py", version=3)
    value = LspDiagnostic(
        document.source.uri,
        LspRange(LspPosition(0, 0), LspPosition(0, 4)),
        severity,
        None,
        "diagnostic",
        (),
    )
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    _patch_stable_attempt(monkeypatch, session, revision, document)
    monkeypatch.setattr(
        session,
        "diagnostics",
        lambda path, *, deadline: ProviderDiagnostics((value,), 3, False),
    )
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DIAGNOSTICS, "pkg/service.py", 1, 0
            ),
            deadline=time.monotonic() + 5,
        )
        assert result.status is NavigationStatus.OK
        assert result.diagnostics[0].severity is expected
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_diagnostic_key_distinguishes_missing_and_empty_codes(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py", version=3)
    range_ = LspRange(LspPosition(0, 0), LspPosition(0, 4))
    diagnostics = (
        LspDiagnostic(
            document.source.uri,
            range_,
            1,
            None,
            "same message",
            (),
        ),
        LspDiagnostic(
            document.source.uri,
            range_,
            1,
            "",
            "same message",
            (),
        ),
    )
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    _patch_stable_attempt(monkeypatch, session, revision, document)
    monkeypatch.setattr(
        session,
        "diagnostics",
        lambda path, *, deadline: ProviderDiagnostics(diagnostics, 3, False),
    )
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DIAGNOSTICS, "pkg/service.py", 1, 0
            ),
            deadline=time.monotonic() + 5,
        )
        assert result.status is NavigationStatus.OK
        assert result.total == 2
        assert {diagnostic.code for diagnostic in result.diagnostics} == {None, ""}
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_empty_unversioned_provider_diagnostics_are_not_ready_not_empty_ok(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py")
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    _patch_stable_attempt(monkeypatch, session, revision, document)
    monkeypatch.setattr(
        session,
        "diagnostics",
        lambda path, *, deadline: ProviderDiagnostics((), None, True),
    )
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DIAGNOSTICS, "pkg/service.py", 1, 0
            ),
            deadline=time.monotonic() + 5,
        )
        assert result.status is NavigationStatus.NOT_READY
        assert result.total == 0
        assert result.diagnostics == ()
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_diagnostic_and_related_facts_share_one_global_cap(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    document = _open_document(scope, "pkg/service.py", version=4)
    api = resolve_repository_source(scope, "pkg/api.py")
    related = tuple(
        (
            LspLocation(
                api.uri,
                LspRange(LspPosition(0, character), LspPosition(0, character)),
            ),
            None,
        )
        for character in range(3)
    )
    value = LspDiagnostic(
        document.source.uri,
        LspRange(LspPosition(0, 0), LspPosition(0, 4)),
        1,
        None,
        "diagnostic",
        related,
    )
    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        structural=lambda request, deadline: (
            _graph_location("pkg/api.py", 6, 15),
        ),
    )
    _patch_stable_attempt(monkeypatch, session, revision, document)
    monkeypatch.setattr(code_navigation, "_MAX_NAVIGATION_FACTS", 3)
    monkeypatch.setattr(
        session,
        "diagnostics",
        lambda path, *, deadline: ProviderDiagnostics((value,), 4, False),
    )
    try:
        result = navigation.query(
            NavigationRequest(
                scope, Capability.DIAGNOSTICS, "pkg/service.py", 1, 0
            ),
            deadline=time.monotonic() + 5,
        )
        assert result.status is NavigationStatus.PARTIAL
        assert result.total == 1
        assert 1 + len(result.diagnostics[0].related) == 3
        assert result.locations == ()
        assert "filtered" in " ".join(result.warnings)
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_resolve_symbol_returns_ambiguity_for_multiple_candidates(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    candidates = (
        _graph_location("pkg/api.py", 6, 15),
        _graph_location("pkg/service.py", 0, 4),
    )
    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        resolver=lambda symbol, repo, deadline: candidates,
    )
    try:
        result = navigation.resolve_symbol(
            "execute", repository=navigation.repository
        )
        assert result.status is NavigationStatus.PARTIAL
        assert result.resolution is ResolutionLabel.AMBIGUOUS
        assert result.total == 2
        assert "disambiguation" in result.warnings[0]
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_resolve_symbol_returns_single_graph_confirmed(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    candidates = (_graph_location("pkg/api.py", 6, 15),)
    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        resolver=lambda symbol, repo, deadline: candidates,
    )
    try:
        result = navigation.resolve_symbol(
            "unique", repository=navigation.repository
        )
        assert result.status is NavigationStatus.OK
        assert result.resolution is ResolutionLabel.GRAPH_CONFIRMED
        assert result.total == 1
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_resolve_symbol_unresolved_when_no_candidates(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        resolver=lambda symbol, repo, deadline: (),
    )
    try:
        result = navigation.resolve_symbol(
            "missing", repository=navigation.repository
        )
        assert result.status is NavigationStatus.PARTIAL
        assert result.resolution is ResolutionLabel.UNRESOLVED
        assert result.total == 0
    finally:
        session.close(deadline=time.monotonic() + 5)


@pytest.mark.parametrize("mutate_every_attempt", [False, True])
def test_resolve_symbol_retries_freshness_once_and_discards_second_race(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
    mutate_every_attempt: bool,
) -> None:
    revisions = (
        _revision(repository, "a"),
        _revision(repository, "b"),
        _revision(repository, "c"),
    )
    current_revision = 0
    resolver_calls = 0
    deadlines: list[float | None] = []

    def compute(
        repository: RepositoryScope, *, deadline: float | None
    ) -> WorkspaceRevision:
        return revisions[current_revision]

    def resolver(
        symbol: str, repository: RepositoryScope, deadline: float | None
    ) -> tuple[NavigationLocation, ...]:
        nonlocal current_revision, resolver_calls
        resolver_calls += 1
        assert symbol == "PublicApi"
        deadlines.append(deadline)
        if resolver_calls == 1 or mutate_every_attempt:
            current_revision += 1
        return (_graph_location("pkg/api.py", 6, 15),)

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        resolver=resolver,
    )
    monkeypatch.setattr(code_navigation, "_compute_revision", compute)
    absolute_deadline = time.monotonic() + 5
    try:
        result = navigation.resolve_symbol(
            "PublicApi",
            repository=navigation.repository,
            deadline=absolute_deadline,
        )
        assert resolver_calls == 2
        assert deadlines == [absolute_deadline, absolute_deadline]
        if mutate_every_attempt:
            assert result.status is NavigationStatus.STALE
            assert result.locations == ()
            assert result.provenance == ()
        else:
            assert result.status is NavigationStatus.OK
            assert result.locations[0].resolution is ResolutionLabel.GRAPH_CONFIRMED
            assert result.locations[0].line == 1
            assert result.locations[0].character == 6
            assert result.workspace_revision_before == revisions[1].revision_sha256
            assert result.workspace_revision_after == revisions[1].revision_sha256
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_resolve_symbol_propagates_keyboard_interrupt(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = _revision(repository, "a")

    def interrupted(
        symbol: str, repository: RepositoryScope, deadline: float | None
    ) -> tuple[NavigationLocation, ...]:
        raise KeyboardInterrupt

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        resolver=interrupted,
    )
    monkeypatch.setattr(
        code_navigation,
        "_compute_revision",
        lambda repository, *, deadline: revision,
    )
    try:
        with pytest.raises(KeyboardInterrupt):
            navigation.resolve_symbol(
                "PublicApi",
                repository=navigation.repository,
                deadline=time.monotonic() + 5,
            )
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_resolve_symbol_stable_sorts_dedupes_validates_and_marks_ambiguity(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = _revision(repository, "a")
    candidates = (
        _graph_location("pkg/api.py", 6, 15),
        _graph_location("pkg/missing.py", 0, 1),
        _graph_location("pkg/api.py", 0, 5),
        _graph_location("pkg/api.py", 6, 15),
    )
    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        resolver=lambda symbol, scope, deadline: candidates,
    )
    monkeypatch.setattr(
        code_navigation,
        "_compute_revision",
        lambda repository, *, deadline: revision,
    )
    try:
        result = navigation.resolve_symbol(
            "PublicApi",
            repository=navigation.repository,
            deadline=time.monotonic() + 5,
        )
        assert result.status is NavigationStatus.PARTIAL
        assert result.resolution is ResolutionLabel.AMBIGUOUS
        assert [location.range for location in result.locations] == [
            PositionRange(0, 5),
            PositionRange(6, 15),
        ]
        assert all(
            location.resolution is ResolutionLabel.AMBIGUOUS
            for location in result.locations
        )
        assert "filtered" in " ".join(result.warnings)
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_resolve_symbol_caps_candidates_and_reports_limit(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (repository / "pkg/many.py").write_bytes(b"x" * 10_001 + b"\n")
    revision = _revision(repository, "a")

    def resolver(
        symbol: str, scope: RepositoryScope, deadline: float | None
    ) -> tuple[NavigationLocation, ...]:
        return tuple(
            _graph_location("pkg/many.py", offset, offset)
            for offset in range(10_001)
        )

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        resolver=resolver,
    )
    monkeypatch.setattr(
        code_navigation,
        "_compute_revision",
        lambda repository, *, deadline: revision,
    )
    try:
        result = navigation.resolve_symbol(
            "many",
            repository=navigation.repository,
            deadline=time.monotonic() + 20,
        )
        assert result.status is NavigationStatus.PARTIAL
        assert result.total == 10_000
        assert len(result.locations) == 10_000
        assert all(
            location.resolution is ResolutionLabel.AMBIGUOUS
            for location in result.locations
        )
        assert "limit" in " ".join(result.warnings)
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_resolve_symbol_input_bound_never_confirms_truncated_singleton(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = _revision(repository, "a")
    candidates = (
        _graph_location("pkg/api.py", 6, 15),
        _graph_location("pkg/service.py", 0, 4),
    )
    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        resolver=lambda symbol, scope, deadline: candidates,
    )
    monkeypatch.setattr(code_navigation, "_MAX_NAVIGATION_INPUT_VALUES", 1)
    monkeypatch.setattr(
        code_navigation,
        "_compute_revision",
        lambda repository, *, deadline: revision,
    )
    try:
        result = navigation.resolve_symbol(
            "PublicApi",
            repository=navigation.repository,
            deadline=time.monotonic() + 5,
        )
        assert result.status is NavigationStatus.PARTIAL
        assert result.total == 1
        assert result.resolution is ResolutionLabel.AMBIGUOUS
        assert result.locations[0].resolution is ResolutionLabel.AMBIGUOUS
        assert "input bound" in " ".join(result.warnings)
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_resolve_symbol_rejects_empty_revision_without_publishing_candidates(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = replace(_revision(repository, "a"), revision_sha256="")
    calls = 0

    def resolver(
        symbol: str, scope: RepositoryScope, deadline: float | None
    ) -> tuple[NavigationLocation, ...]:
        nonlocal calls
        calls += 1
        return (_graph_location("pkg/api.py", 6, 15),)

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        resolver=resolver,
    )
    monkeypatch.setattr(
        code_navigation,
        "_compute_revision",
        lambda repository, *, deadline: revision,
    )
    try:
        result = navigation.resolve_symbol(
            "PublicApi",
            repository=navigation.repository,
            deadline=time.monotonic() + 5,
        )
        assert result.status is NavigationStatus.ERROR
        assert result.locations == ()
        assert calls == 0
    finally:
        session.close(deadline=time.monotonic() + 5)


@pytest.mark.parametrize("verified", [False, True])
def test_verify_edge_uses_both_anchors_and_only_true_confirms_target(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
    verified: bool,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    source = _source_anchor(scope, "pkg/service.py", 10, 15)
    target = _source_anchor(scope, "pkg/api.py", 2, 8)
    calls: list[tuple[SourceAnchor, SourceAnchor, RepositoryScope, float]] = []

    def verifier(
        source_anchor: SourceAnchor,
        target_anchor: SourceAnchor,
        repository_scope: RepositoryScope,
        deadline: float,
    ) -> bool:
        calls.append((source_anchor, target_anchor, repository_scope, deadline))
        return verified

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        edge=verifier,
    )
    monkeypatch.setattr(
        code_navigation,
        "_compute_revision",
        lambda repository, *, deadline: revision,
    )
    absolute_deadline = time.monotonic() + 5
    before_files = {path.relative_to(state_root) for path in state_root.rglob("*")}
    try:
        result = navigation.verify_edge(
            source,
            target,
            repository=scope,
            deadline=absolute_deadline,
        )
        assert calls == [(source, target, scope, absolute_deadline)]
        assert {path.relative_to(state_root) for path in state_root.rglob("*")} == before_files
        if verified:
            assert result.status is NavigationStatus.OK
            assert result.resolution is ResolutionLabel.GRAPH_CONFIRMED
            assert len(result.locations) == 1
            location = result.locations[0]
            assert location.path == target.path
            assert location.range == PositionRange(target.byte_offset, target.byte_offset)
            assert location.line == target.line
            assert location.character == target.utf8_character
        else:
            assert result.status is NavigationStatus.PARTIAL
            assert result.resolution is ResolutionLabel.UNRESOLVED
            assert result.locations == ()
            assert "no structural edge proof" in result.warnings
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_verify_edge_without_verifier_is_partial_not_deadness_proof(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    source = _source_anchor(scope, "pkg/service.py", 10, 15)
    target = _source_anchor(scope, "pkg/api.py", 2, 8)
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    monkeypatch.setattr(
        code_navigation,
        "_compute_revision",
        lambda repository, *, deadline: revision,
    )
    try:
        result = navigation.verify_edge(
            source,
            target,
            repository=scope,
            deadline=time.monotonic() + 5,
        )
        assert result.status is NavigationStatus.PARTIAL
        assert result.resolution is ResolutionLabel.UNRESOLVED
        assert result.locations == ()
    finally:
        session.close(deadline=time.monotonic() + 5)


@pytest.mark.parametrize("mutate_every_attempt", [False, True])
def test_verify_edge_retries_freshness_once_and_discards_second_race(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
    mutate_every_attempt: bool,
) -> None:
    scope = resolve_repository_scope(repository)
    revisions = (
        _revision(repository, "a"),
        _revision(repository, "b"),
        _revision(repository, "c"),
    )
    source = _source_anchor(scope, "pkg/service.py", 10, 15)
    target = _source_anchor(scope, "pkg/api.py", 2, 8)
    current_revision = 0
    verifier_calls = 0

    def compute(repository: RepositoryScope, *, deadline: float) -> WorkspaceRevision:
        return revisions[current_revision]

    def verifier(
        source_anchor: SourceAnchor,
        target_anchor: SourceAnchor,
        repository_scope: RepositoryScope,
        deadline: float,
    ) -> bool:
        nonlocal current_revision, verifier_calls
        verifier_calls += 1
        assert source_anchor is source
        assert target_anchor is target
        if verifier_calls == 1 or mutate_every_attempt:
            current_revision += 1
        return True

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        edge=verifier,
    )
    monkeypatch.setattr(code_navigation, "_compute_revision", compute)
    try:
        result = navigation.verify_edge(
            source,
            target,
            repository=scope,
            deadline=time.monotonic() + 5,
        )
        assert verifier_calls == 2
        if mutate_every_attempt:
            assert result.status is NavigationStatus.STALE
            assert result.locations == ()
            assert result.provenance == ()
        else:
            assert result.status is NavigationStatus.OK
            assert result.workspace_revision_before == revisions[1].revision_sha256
            assert result.workspace_revision_after == revisions[1].revision_sha256
    finally:
        session.close(deadline=time.monotonic() + 5)


@pytest.mark.parametrize(
    "failure_type",
    [FileNotFoundError, PermissionError, OSError],
)
@pytest.mark.parametrize("mutate_every_attempt", [False, True])
def test_verify_edge_retries_resolution_failure_for_pre_revision_entry(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[OSError],
    mutate_every_attempt: bool,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    source = _source_anchor(scope, "pkg/service.py", 10, 15)
    target = _source_anchor(scope, "pkg/api.py", 2, 8)
    original_resolve = code_navigation.resolve_repository_source
    target_resolutions = 0
    verifier_calls = 0

    def resolve(
        repository_scope: RepositoryScope, path: str
    ):
        nonlocal target_resolutions
        if path == target.path:
            target_resolutions += 1
            if target_resolutions == 1 or mutate_every_attempt:
                raise failure_type("target changed after revision")
        return original_resolve(repository_scope, path)

    def verifier(*args) -> bool:
        nonlocal verifier_calls
        verifier_calls += 1
        return True

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        edge=verifier,
    )
    monkeypatch.setattr(
        code_navigation,
        "_compute_revision",
        lambda repository, *, deadline: revision,
    )
    monkeypatch.setattr(code_navigation, "resolve_repository_source", resolve)
    try:
        result = navigation.verify_edge(
            source,
            target,
            repository=scope,
            deadline=time.monotonic() + 5,
        )
        if mutate_every_attempt:
            assert target_resolutions == 2
            assert verifier_calls == 0
            assert result.status is NavigationStatus.STALE
            assert result.locations == ()
            assert result.provenance == ()
        else:
            assert target_resolutions == 2
            assert verifier_calls == 1
            assert result.status is NavigationStatus.OK
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_verify_edge_missing_from_fresh_revision_is_error_after_post_fence(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    source = _source_anchor(scope, "pkg/service.py", 10, 15)
    target = replace(_source_anchor(scope, "pkg/api.py", 2, 8), path="pkg/missing.py")
    revision_calls = 0

    def compute(
        repository_scope: RepositoryScope, *, deadline: float
    ) -> WorkspaceRevision:
        nonlocal revision_calls
        revision_calls += 1
        return revision

    navigation, session = _navigation(repository, state_root, semantic_pyright)
    monkeypatch.setattr(code_navigation, "_compute_revision", compute)
    try:
        result = navigation.verify_edge(
            source,
            target,
            repository=scope,
            deadline=time.monotonic() + 5,
        )
        assert result.status is NavigationStatus.ERROR
        assert revision_calls == 2
    finally:
        session.close(deadline=time.monotonic() + 5)


@pytest.mark.parametrize("appeared_anchor", ["source", "target"])
def test_verify_edge_retries_when_either_anchor_first_appears_inside_revision_fence(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
    appeared_anchor: str,
) -> None:
    scope = resolve_repository_scope(repository)
    relative_path = f"pkg/appeared_{appeared_anchor}.py"
    target_path = repository / relative_path
    content = b"appeared = True\n"
    appeared = SourceDocument.from_bytes(relative_path, content).validate_anchor(
        line=1,
        character=0,
    )
    existing_source = _source_anchor(scope, "pkg/service.py", 10, 15)
    existing_target = _source_anchor(scope, "pkg/api.py", 2, 8)
    source = appeared if appeared_anchor == "source" else existing_source
    target = appeared if appeared_anchor == "target" else existing_target
    original_resolve = code_navigation.resolve_repository_source
    verifier_calls = 0

    def resolve(repository_scope: RepositoryScope, path: str):
        if path == relative_path and not target_path.exists():
            target_path.write_bytes(content)
        return original_resolve(repository_scope, path)

    def verifier(*args) -> bool:
        nonlocal verifier_calls
        verifier_calls += 1
        return True

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        edge=verifier,
    )
    monkeypatch.setattr(code_navigation, "resolve_repository_source", resolve)
    try:
        result = navigation.verify_edge(
            source,
            target,
            repository=scope,
            deadline=time.monotonic() + 5,
        )

        assert result.status is NavigationStatus.OK
        assert result.workspace_revision_before == result.workspace_revision_after
        assert verifier_calls == 1
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_verify_edge_validates_both_anchors_before_callback_and_propagates_interrupt(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    source = _source_anchor(scope, "pkg/service.py", 10, 15)
    target = _source_anchor(scope, "pkg/api.py", 2, 8)
    calls = 0

    def interrupted(
        source_anchor: SourceAnchor,
        target_anchor: SourceAnchor,
        repository_scope: RepositoryScope,
        deadline: float,
    ) -> bool:
        nonlocal calls
        calls += 1
        raise KeyboardInterrupt

    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        edge=interrupted,
    )
    monkeypatch.setattr(
        code_navigation,
        "_compute_revision",
        lambda repository, *, deadline: revision,
    )
    try:
        invalid_target = replace(target, byte_offset=target.byte_offset + 1)
        result = navigation.verify_edge(
            source,
            invalid_target,
            repository=scope,
            deadline=time.monotonic() + 5,
        )
        assert result.status is NavigationStatus.ERROR
        assert calls == 0
        with pytest.raises(KeyboardInterrupt):
            navigation.verify_edge(
                source,
                target,
                repository=scope,
                deadline=time.monotonic() + 5,
            )
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_graph_candidates_appended_after_lsp_results(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    from code_navigation import _graph_only_candidates

    lsp = (
        NavigationLocation(
            "pkg/a.py",
            PositionRange(0, 4),
            1,
            0,
            None,
            None,
            ResolutionLabel.LSP_CONFIRMED,
            (Provenance("lsp", "pyright", "1.1.411", "provider_reported"),),
        ),
    )
    graph = (
        _graph_location("pkg/a.py", 0, 4),
        _graph_location("pkg/b.py", 10, 14),
    )
    graph_provenance = (
        Provenance("graph", "evidence-graph", "structural", "graph_candidate"),
    )
    appended = _graph_only_candidates(graph, lsp, graph_provenance)
    assert len(appended) == 1
    assert appended[0].path == "pkg/b.py"
    assert appended[0].resolution is ResolutionLabel.GRAPH_CANDIDATE


def test_dedupe_locations_collapses_duplicates(
    repository: Path,
) -> None:
    from code_navigation import _dedupe_locations

    location = NavigationLocation(
        "pkg/a.py",
        PositionRange(0, 4),
        1,
        0,
        None,
        None,
        ResolutionLabel.LSP_CONFIRMED,
        (Provenance("lsp", "pyright", "1.1.411", "provider_reported"),),
    )
    result = _dedupe_locations((location, location))
    assert len(result) == 1


def test_dedupe_uses_mandated_identity_and_complete_deterministic_tie_break(
    repository: Path,
) -> None:
    from code_navigation import _dedupe_locations, _location_key

    location = NavigationLocation(
        "pkg/a.py",
        PositionRange(0, 4),
        1,
        0,
        None,
        None,
        ResolutionLabel.LSP_CONFIRMED,
        (Provenance("lsp", "pyright", "1.1.411", "provider_reported"),),
    )
    values = (
        location,
        replace(location, line=2),
        replace(location, character=1),
        replace(location, containing_symbol=""),
        replace(location, signature=""),
        replace(
            location,
            provenance=(
                Provenance("lsp", "pyright", "1.1.411", "zzz-observation"),
            ),
        ),
    )
    assert _location_key(location) == (
        "pkg/a.py",
        0,
        4,
        ResolutionLabel.LSP_CONFIRMED.value,
        "pyright",
    )
    assert _dedupe_locations(values) == (location,)
    assert _dedupe_locations(tuple(reversed(values))) == (location,)
    first = Provenance("lsp", "pyright", "1.1.411", "a-observation")
    second = Provenance("lsp", "pyright", "1.1.411", "b-observation")
    ordered_provenance = replace(location, provenance=(first, second))
    reversed_provenance = replace(location, provenance=(second, first))
    assert _dedupe_locations(
        (reversed_provenance, ordered_provenance)
    ) == (ordered_provenance,)


@pytest.mark.parametrize("label", list(ResolutionLabel))
def test_every_resolution_label_is_constructible(
    label: ResolutionLabel, repository: Path,
) -> None:
    location = NavigationLocation(
        "pkg/a.py",
        PositionRange(0, 4),
        1,
        0,
        None,
        None,
        label,
        (Provenance("lsp", "pyright", "1.1.411", "obs"),),
    )
    assert location.resolution is label


@pytest.mark.parametrize("status", list(NavigationStatus))
def test_every_status_is_constructible(
    status: NavigationStatus, repository: Path,
) -> None:
    scope = _scope(repository)
    result = NavigationResult(
        status,
        Capability.DEFINITIONS,
        Capability.DEFINITIONS,
        "pyright",
        "1.1.411",
        scope.repository_id,
        scope.checkout_id,
        "abc",
        "abc",
        1,
        PositionEncoding.UTF8,
        "query_ready",
        None,
        0,
        0,
        10,
        (),
        (),
        None,
        ResolutionLabel.UNRESOLVED,
        (),
        (),
    )
    assert result.status is status


def test_empty_lsp_result_is_provider_reported_not_deadness(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"responses": {"textDocument/definition": None}},
    )
    navigation, session = _navigation(repository, state_root, fixture)
    try:
        session.start(deadline=time.monotonic() + 10)
        session.open_document("pkg/service.py", deadline=time.monotonic() + 10)
        request = NavigationRequest(
            resolve_repository_scope(repository),
            Capability.DEFINITIONS,
            "pkg/service.py",
            10,
            20,
        )
        result = navigation.query(request, deadline=time.monotonic() + 30)
        assert result.status is NavigationStatus.OK
        assert result.locations == ()
        assert result.resolution is ResolutionLabel.UNRESOLVED
        assert result.provider == "pyright"
    finally:
        navigation.close(deadline=time.monotonic() + 5)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    from tests.code_kernel_helpers import create_python_repository

    return create_python_repository(tmp_path / "repository")


@pytest.fixture
def state_root(tmp_path: Path) -> Path:
    from reliable_memory import validate_state_root

    root = tmp_path / "state"
    validate_state_root(root)
    return root


@pytest.fixture
def semantic_pyright(repository: Path) -> SemanticPyrightFixture:
    return create_semantic_pyright_fixture(repository)
