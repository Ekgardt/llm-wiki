"""Normalized code navigation contract and facade tests."""

from __future__ import annotations

import time
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from code_intelligence import Capability, PositionEncoding, PositionRange
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
from pyright_session import PyrightSession
from repository_scope import RepositoryScope, resolve_repository_scope

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
        1,
        "code",
        "message",
        (),
        provenance,
    )
    with pytest.raises(FrozenInstanceError):
        diagnostic.message = "other"  # type: ignore[misc]


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
) -> tuple[CodeNavigation, PyrightSession]:
    scope = resolve_repository_scope(repository)
    session = PyrightSession(scope, fixture.identity, state_root=state_root)
    navigation = CodeNavigation(
        scope,
        session,
        fixture.identity,
        structural_candidates=structural,
        symbol_resolver=resolver,
    )
    return navigation, session


def test_resolve_symbol_returns_ambiguity_for_multiple_candidates(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,
) -> None:
    candidates = (
        _graph_location("pkg/a.py", 0, 4),
        _graph_location("pkg/b.py", 10, 14),
    )
    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        resolver=lambda symbol, repo: candidates,
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
    candidates = (_graph_location("pkg/a.py", 0, 4),)
    navigation, session = _navigation(
        repository,
        state_root,
        semantic_pyright,
        resolver=lambda symbol, repo: candidates,
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
        resolver=lambda symbol, repo: (),
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
