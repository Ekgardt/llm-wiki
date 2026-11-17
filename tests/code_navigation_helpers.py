"""Helpers shared by test_code_navigation.py and the test files importing them."""
from __future__ import annotations

from pathlib import Path

import pytest
from code_navigation import CodeNavigation
from llm_wiki.pyright_session_core import PyrightSession
from repository_scope import resolve_repository_scope

from tests.code_kernel_helpers import SemanticPyrightFixture, create_semantic_pyright_fixture


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
