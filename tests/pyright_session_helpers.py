"""Helpers shared by the test_pyright_session*.py files and the test files importing them."""
from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest
from llm_wiki.pyright_session_core import PyrightSession
from lsp_positions import SourceAnchor, SourceDocument
from pyright_profile import PyrightIdentity
from repository_scope import resolve_repository_scope

from tests.code_kernel_helpers import SemanticPyrightFixture, create_semantic_pyright_fixture


@pytest.fixture
def semantic_pyright(repository: Path) -> SemanticPyrightFixture:
    return create_semantic_pyright_fixture(repository)


def _session(
    repository: Path,
    state_root: Path,
    fixture: SemanticPyrightFixture,
) -> PyrightSession:
    return PyrightSession(
        resolve_repository_scope(repository),
        fixture.identity,  # type: ignore[arg-type]
        state_root=state_root,
    )


def _anchor(repository: Path, path: str, line: int, character: int) -> SourceAnchor:
    return SourceDocument.from_bytes(path, (repository / path).read_bytes()).validate_anchor(
        line=line,
        character=character,
    )


def _missing_identity() -> PyrightIdentity:
    return PyrightIdentity(
        status="missing",
        source=None,
        version=None,
        node_executable=None,
        node_version=None,
        node_major=None,
        server_executable=None,
        executable_sha256=None,
        package_sha256=None,
        initialization_options_sha256="a" * 64,
        configuration_sha256="b" * 64,
        qualified=False,
        degradation_codes=("pyright_missing",),
    )


def _copied_server_identity(
    repository: Path,
    fixture: SemanticPyrightFixture,
) -> tuple[Path, PyrightIdentity]:
    source = Path(fixture.identity.server_executable)  # type: ignore[union-attr]
    server = repository / "qualified-server.py"
    content = source.read_bytes()
    server.write_bytes(content)
    return server, replace(
        fixture.identity,
        server_executable=server,
        executable_sha256=hashlib.sha256(content).hexdigest(),
    )


def _client_events(events) -> list[dict]:
    return [event for event in events if event["kind"] == "client-message"]


def _client_pids(events) -> tuple[int, ...]:
    """Distinct server processes that received client messages, in order."""
    return tuple(
        dict.fromkeys(
            event["pid"] for event in events if event["kind"] == "client-message"
        )
    )


def _join_all(threads, timeout: float = 5) -> None:
    for thread in threads:
        thread.join(timeout)
    assert all(not thread.is_alive() for thread in threads)


def _client_methods(fixture) -> list[str]:
    return [
        event["method"]
        for event in fixture.events()
        if event["kind"] == "client-message"
    ]


# The subject must return on its own deadline while the lock is still held, so
# the holder keeps holding until the test says otherwise. A fixed sleep raced
# the assertions on a loaded macOS runner and the hold ended first.
CONTENTION_HOLD_SECONDS = 60.0
CONTENTION_RETURN_SECONDS = 5.0
