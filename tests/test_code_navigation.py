"""Normalized code navigation contract and facade tests."""

from __future__ import annotations

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
from lsp_positions import LspPosition, LspRange, SourceDocument
from lsp_security import resolve_repository_source
from pyright_session import (
    LspLocation,
    ProviderLocations,
    PyrightSession,
)
from repository_scope import RepositoryScope, resolve_repository_scope
from workspace_revision import compute_workspace_revision

from tests.code_kernel_helpers import (
    SemanticPyrightFixture,
    create_semantic_pyright_fixture,
)
from tests.code_navigation_helpers import (
    _navigation,
    repository,  # noqa: F401
    semantic_pyright,  # noqa: F401
    state_root,  # noqa: F401
)
from tests.code_navigation_query_helpers import (
    _graph_location,
    _open_document,
    _patch_stable_attempt,
    _revision,
)
from tests.slow_machine import SHORT_TIMEOUT


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


def _scope(repository: Path) -> RepositoryScope:  # noqa: F811
    return resolve_repository_scope(repository)


def test_navigation_request_validates_required_fields(
    repository: Path,  # noqa: F811
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
    repository: Path,  # noqa: F811
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
    repository: Path,  # noqa: F811
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
    repository: Path,  # noqa: F811
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


def _invalid_provenance_scalar(provenance: Provenance, location: object) -> None:
    del provenance, location
    Provenance("lsp", object(), "1.1.411", "provider_reported")  # type: ignore[arg-type]


def _invalid_location_scalar(provenance: Provenance, location: object) -> None:
    del location
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


def _invalid_location_provenance(provenance: Provenance, location: object) -> None:
    del provenance, location
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


def _invalid_diagnostic_related(provenance: Provenance, location: object) -> None:
    del location
    NavigationDiagnostic(
        "pkg/api.py",
        PositionRange(0, 4),
        DiagnosticSeverity.ERROR,
        None,
        "message",
        (object(),),  # type: ignore[arg-type]
        (provenance,),
    )


def _invalid_diagnostic_provenance(provenance: Provenance, location: object) -> None:
    del provenance
    NavigationDiagnostic(
        "pkg/api.py",
        PositionRange(0, 4),
        DiagnosticSeverity.ERROR,
        None,
        "message",
        (location,),  # type: ignore[arg-type]
        (object(),),  # type: ignore[arg-type]
    )


_INVALID_RECORD_BUILDERS = {
    "provenance_scalar": _invalid_provenance_scalar,
    "location_scalar": _invalid_location_scalar,
    "location_provenance": _invalid_location_provenance,
    "diagnostic_related": _invalid_diagnostic_related,
    "diagnostic_provenance": _invalid_diagnostic_provenance,
}


def _invalid_field_value(field: str) -> object:
    """A value of the wrong type for that field: a string where an enum belongs."""
    if field == "status":
        return "partial"
    return (object(),)


def _build_invalid_record(
    invalid_record: str,
    result_values: dict,
    provenance: Provenance,
    location: object,
) -> None:
    """Construct the record the case names, with exactly one field of a wrong type."""
    builder = _INVALID_RECORD_BUILDERS.get(invalid_record)
    if builder is not None:
        builder(provenance, location)
        return
    field = invalid_record.removeprefix("result_")
    result_values[field] = _invalid_field_value(field)
    NavigationResult(**result_values)  # type: ignore[arg-type]


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
    repository: Path,  # noqa: F811
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
        _build_invalid_record(invalid_record, result_values, provenance, location)


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
    repository: Path,  # noqa: F811
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
            return
        if record_kind == "location":
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
            return
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
    repository: Path,  # noqa: F811
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
    repository: Path,  # noqa: F811
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
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
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
            navigation.query(request, deadline=time.monotonic() + SHORT_TIMEOUT)
    finally:
        navigation.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_code_navigation_type_checks_constructor(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
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
    session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_code_navigation_binds_exact_session_identity_and_repository(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
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
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_post_revision_reuses_exact_verified_revision(
    repository: Path,  # noqa: F811
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
        "compute_revision",
        lambda value, *, deadline: (_ for _ in ()).throw(AssertionError("full recompute")),
    )

    current = code_navigation.compute_post_revision(
        scope,
        expected,
        deadline=time.monotonic() + SHORT_TIMEOUT,
    )

    assert current is expected
    assert events == ["verify"]


def test_post_revision_mismatch_falls_back_to_full_recomputation(
    repository: Path,  # noqa: F811
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
        "compute_revision",
        lambda value, *, deadline: current,
    )

    assert (
        code_navigation.compute_post_revision(
            scope,
            expected,
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        is current
    )


def test_stable_queries_reuse_content_addressed_source_documents(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
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
    parsed = _record_parsed_documents(monkeypatch)
    request = NavigationRequest(
        scope,
        Capability.DEFINITIONS,
        "pkg/service.py",
        10,
        20,
    )
    try:
        first = navigation.query(request, deadline=time.monotonic() + SHORT_TIMEOUT)
        second = navigation.query(request, deadline=time.monotonic() + SHORT_TIMEOUT)

        assert first.status is NavigationStatus.OK
        assert second.status is NavigationStatus.OK
        assert parsed.count("pkg/service.py") == 1
        assert parsed.count("pkg/api.py") == 1
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def _write_cache_sources(repository: Path, prefix: str, names: tuple) -> tuple:  # noqa: F811
    """Write one tiny module per name and return their repository paths."""
    paths = tuple(f"pkg/{prefix}_{name}.py" for name in names)
    for index, path in enumerate(paths):
        (repository / path).write_text(f"value = {index}\n", encoding="utf-8")
    return paths


def _record_parsed_documents(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Collect the path of every document the cache actually parses."""
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
    return parsed


def _cache_sources_and_documents(
    scope: RepositoryScope,
    repository: Path,  # noqa: F811
    paths: tuple,
) -> tuple[dict, dict]:
    """The repository source and the parsed document behind each path."""
    sources = {path: resolve_repository_source(scope, path) for path in paths}
    documents = {
        path: SourceDocument.from_bytes(path, (repository / path).read_bytes())
        for path in paths
    }
    return sources, documents


def _publish_consumed_documents(
    navigation: CodeNavigation,
    revision_entries: dict,
    sources: dict,
    documents: dict,
    paths: tuple,
) -> None:
    """Fill the shared cache with these documents, every one of them consumed."""
    from code_navigation import AttemptDocuments

    initial = AttemptDocuments()
    for path in paths:
        initial[sources[path].uri] = documents[path]
        initial.consume(sources[path].uri, documents[path])
    navigation._publish_source_documents(revision_entries, initial)


def test_source_document_cache_lru_tracks_only_consumed_documents(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _write_cache_sources(repository, "cache", tuple(range(4)))
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    open_documents = {path: _open_document(scope, path) for path in paths}
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    _patch_stable_attempt(monkeypatch, session, revision, open_documents[paths[0]])
    monkeypatch.setattr(code_navigation, "MAX_SOURCE_DOCUMENT_CACHE_ENTRIES", 3)
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
    parsed = _record_parsed_documents(monkeypatch)

    def query(path: str) -> None:
        result = navigation.query(
            NavigationRequest(scope, Capability.DEFINITIONS, path, 1, 0),
            deadline=time.monotonic() + SHORT_TIMEOUT,
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
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_source_document_cache_repeated_use_refreshes_attempt_recency(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _write_cache_sources(repository, "repeated", ("a", "b", "c"))
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    request_document = _open_document(scope, paths[0])
    sources = {path: resolve_repository_source(scope, path) for path in paths}
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    _patch_stable_attempt(monkeypatch, session, revision, request_document)
    monkeypatch.setattr(code_navigation, "MAX_SOURCE_DOCUMENT_CACHE_ENTRIES", 2)
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
    parsed = _record_parsed_documents(monkeypatch)
    request = NavigationRequest(scope, Capability.DEFINITIONS, paths[0], 1, 0)
    try:
        first = navigation.query(request, deadline=time.monotonic() + SHORT_TIMEOUT)
        second = navigation.query(request, deadline=time.monotonic() + SHORT_TIMEOUT)

        assert first.status is NavigationStatus.OK
        assert second.status is NavigationStatus.OK
        assert parsed.count(paths[0]) == 1
        assert parsed.count(paths[1]) == 2
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_source_document_cache_rejects_newline_dense_document_over_byte_bound(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = "pkg/newline_dense.py"
    (repository / path).write_bytes(b"x\n" * 8)
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    request_document = _open_document(scope, path)
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    _patch_stable_attempt(monkeypatch, session, revision, request_document)
    monkeypatch.setattr(code_navigation, "MAX_SOURCE_DOCUMENT_CACHE_BYTES", 1024)
    monkeypatch.setattr(
        session,
        "definition",
        lambda anchor, *, deadline: ProviderLocations(
            (), "provider_reported", False
        ),
    )
    parsed = _record_parsed_documents(monkeypatch)
    request = NavigationRequest(scope, Capability.DEFINITIONS, path, 1, 0)
    try:
        first = navigation.query(request, deadline=time.monotonic() + SHORT_TIMEOUT)
        second = navigation.query(request, deadline=time.monotonic() + SHORT_TIMEOUT)

        assert first.status is NavigationStatus.OK
        assert second.status is NavigationStatus.OK
        assert parsed.count(path) == 2
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_source_document_cache_rejects_path_and_key_heavy_document_over_byte_bound(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = f"pkg/retained_{'x' * 80}.py"
    (repository / path).write_text("value = 1\n", encoding="utf-8")
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    request_document = _open_document(scope, path)
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    _patch_stable_attempt(monkeypatch, session, revision, request_document)
    monkeypatch.setattr(code_navigation, "MAX_SOURCE_DOCUMENT_CACHE_BYTES", 1600)
    monkeypatch.setattr(
        session,
        "definition",
        lambda anchor, *, deadline: ProviderLocations(
            (), "provider_reported", False
        ),
    )
    parsed = _record_parsed_documents(monkeypatch)
    request = NavigationRequest(scope, Capability.DEFINITIONS, path, 1, 0)
    try:
        first = navigation.query(request, deadline=time.monotonic() + SHORT_TIMEOUT)
        second = navigation.query(request, deadline=time.monotonic() + SHORT_TIMEOUT)

        assert first.status is NavigationStatus.OK
        assert second.status is NavigationStatus.OK
        assert parsed.count(path) == 2
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_source_document_cache_concurrent_publication_preserves_access_order(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _write_cache_sources(repository, "concurrent", ("a", "b", "c"))
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    revision_entries = {entry.path: entry for entry in revision.entries}
    sources, documents = _cache_sources_and_documents(scope, repository, paths)
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    monkeypatch.setattr(code_navigation, "MAX_SOURCE_DOCUMENT_CACHE_ENTRIES", 2)
    _publish_consumed_documents(
        navigation, revision_entries, sources, documents, paths[:2]
    )

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
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_source_document_cache_consume_after_eviction_republishes_when_stable(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _write_cache_sources(repository, "inverse", ("a", "b", "c"))
    scope = resolve_repository_scope(repository)
    revision = _revision(repository, "a")
    revision_entries = {entry.path: entry for entry in revision.entries}
    sources, documents = _cache_sources_and_documents(scope, repository, paths)
    navigation, session = _navigation(repository, state_root, semantic_pyright)
    monkeypatch.setattr(code_navigation, "MAX_SOURCE_DOCUMENT_CACHE_ENTRIES", 2)
    _publish_consumed_documents(
        navigation, revision_entries, sources, documents, paths[:2]
    )

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
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_source_document_cache_structural_alias_refreshes_recency(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
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
    monkeypatch.setattr(code_navigation, "MAX_SOURCE_DOCUMENT_CACHE_ENTRIES", 2)
    monkeypatch.setattr(
        session,
        "definition",
        lambda anchor, *, deadline: ProviderLocations(
            (), "provider_reported", False
        ),
    )
    parsed = _record_parsed_documents(monkeypatch)
    request = NavigationRequest(scope, Capability.DEFINITIONS, paths[0], 1, 0)
    try:
        first = navigation.query(request, deadline=time.monotonic() + SHORT_TIMEOUT)
        second = navigation.query(request, deadline=time.monotonic() + SHORT_TIMEOUT)

        assert first.status is NavigationStatus.PARTIAL
        assert second.status is NavigationStatus.PARTIAL
        assert parsed.count(paths[0]) == 1
        assert parsed.count(paths[1]) == 2
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_code_navigation_close_clears_source_cache_when_session_close_fails(
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
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
        deadline=time.monotonic() + SHORT_TIMEOUT,
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
            navigation.close(deadline=time.monotonic() + SHORT_TIMEOUT)
        assert not navigation._source_document_cache
        assert navigation._source_document_cache_bytes == 0
    finally:
        real_close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_dedupe_locations_collapses_duplicates(
    repository: Path,  # noqa: F811
) -> None:
    from code_navigation import dedupe_locations

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
    result = dedupe_locations((location, location))
    assert len(result) == 1


def test_dedupe_uses_mandated_identity_and_complete_deterministic_tie_break(
    repository: Path,  # noqa: F811
) -> None:
    from code_navigation import dedupe_locations, location_key

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
    assert location_key(location) == (
        "pkg/a.py",
        0,
        4,
        ResolutionLabel.LSP_CONFIRMED.value,
        "pyright",
    )
    assert dedupe_locations(values) == (location,)
    assert dedupe_locations(tuple(reversed(values))) == (location,)
    first = Provenance("lsp", "pyright", "1.1.411", "a-observation")
    second = Provenance("lsp", "pyright", "1.1.411", "b-observation")
    ordered_provenance = replace(location, provenance=(first, second))
    reversed_provenance = replace(location, provenance=(second, first))
    assert dedupe_locations(
        (reversed_provenance, ordered_provenance)
    ) == (ordered_provenance,)


@pytest.mark.parametrize("label", list(ResolutionLabel))
def test_every_resolution_label_is_constructible(
    label: ResolutionLabel, repository: Path,  # noqa: F811
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
    status: NavigationStatus, repository: Path,  # noqa: F811
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
    repository: Path,  # noqa: F811
    state_root: Path,  # noqa: F811
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"responses": {"textDocument/definition": None}},
    )
    navigation, session = _navigation(repository, state_root, fixture)
    try:
        session.start(deadline=time.monotonic() + SHORT_TIMEOUT)
        session.open_document("pkg/service.py", deadline=time.monotonic() + SHORT_TIMEOUT)
        request = NavigationRequest(
            resolve_repository_scope(repository),
            Capability.DEFINITIONS,
            "pkg/service.py",
            10,
            20,
        )
        result = navigation.query(request, deadline=time.monotonic() + SHORT_TIMEOUT)
        assert result.status is NavigationStatus.OK
        assert result.locations == ()
        assert result.resolution is ResolutionLabel.UNRESOLVED
        assert result.provider == "pyright"
    finally:
        navigation.close(deadline=time.monotonic() + SHORT_TIMEOUT)
