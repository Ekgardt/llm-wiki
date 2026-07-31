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
