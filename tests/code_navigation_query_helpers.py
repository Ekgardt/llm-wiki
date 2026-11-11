"""Navigation fixtures the code navigation query test files share."""
from __future__ import annotations

import hashlib
import time
from dataclasses import replace
from pathlib import Path

import code_navigation
import pytest
from code_intelligence import (
    Capability,
    PositionEncoding,
    PositionRange,
)
from code_navigation import (
    NavigationLocation,
    NavigationRequest,
    Provenance,
    ResolutionLabel,
)
from lsp_positions import SourceAnchor, SourceDocument
from lsp_security import resolve_repository_source
from pyright_session import (
    OpenDocument,
    ProviderLocations,
    PyrightSession,
)
from repository_scope import RepositoryScope, resolve_repository_scope
from workspace_revision import WorkspaceRevision, compute_workspace_revision

from tests.slow_machine import SHORT_TIMEOUT


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


def _revision(repository: Path, marker: str) -> WorkspaceRevision:  # noqa: F811
    revision = compute_workspace_revision(
        resolve_repository_scope(repository), deadline=time.monotonic() + SHORT_TIMEOUT
    )
    return replace(revision, revision_sha256=marker * 64)


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
        "compute_revision",
        lambda repository, *, deadline: revision,  # noqa: F811
    )
    monkeypatch.setattr(session, "synchronize", lambda value, *, deadline: None)
    monkeypatch.setattr(
        session,
        "open_document",
        lambda path, *, deadline: document,
    )


_OPERATION_ALIASES = {
    "structural": "query",
    "query": "query",
    "provider_target": "query",
    "resolver": "resolve_symbol",
    "resolve_symbol": "resolve_symbol",
    "edge": "verify_edge",
    "verify_edge": "verify_edge",
}


def _navigation_operation(kind: str, navigation, scope, deadline: float):
    """Run the operation this case names, with the deadline it was given."""
    runners = {
        "query": lambda: navigation.query(
            NavigationRequest(
                scope, Capability.DEFINITIONS, "pkg/service.py", 10, 20
            ),
            deadline=deadline,
        ),
        "resolve_symbol": lambda: navigation.resolve_symbol(
            "PublicApi", repository=scope, deadline=deadline
        ),
        "verify_edge": lambda: navigation.verify_edge(
            _source_anchor(scope, "pkg/service.py", 10, 15),
            _source_anchor(scope, "pkg/api.py", 2, 8),
            repository=scope,
            deadline=deadline,
        ),
    }
    return runners[_OPERATION_ALIASES[kind]]()


def _callback_slots(callback_kind: str, callback) -> dict:
    """Hand the callback to the one seam this case is about, and to no other."""
    slots = dict.fromkeys(("structural", "resolver", "edge"))
    slots[callback_kind] = callback
    return slots


def _patch_empty_definitions(
    monkeypatch: pytest.MonkeyPatch,
    session,
    document,
) -> None:
    """A synchronized session whose provider answers with no location at all."""
    session._position_encoding = PositionEncoding.UTF8
    monkeypatch.setattr(session, "synchronize", lambda value, *, deadline: None)
    monkeypatch.setattr(session, "open_document", lambda path, *, deadline: document)
    monkeypatch.setattr(
        session,
        "definition",
        lambda anchor, *, deadline: ProviderLocations(
            (), "provider_reported", False
        ),
    )
