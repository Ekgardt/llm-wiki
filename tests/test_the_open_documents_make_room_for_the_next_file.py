"""Open documents are evicted, and an unmoved file is not read twice.

Finding K-A12 of the 2026-09-17 audit: documents left the open set only when
the workspace deleted or renamed them, so the file after the 256th raised
`RuntimeError` and `code_navigation` fell back to structural answers for every
new file for the life of the process; and every `synchronize` -- one per query
-- re-read and re-hashed every open document.
Research: docs/research/2026-09-17-sess-open-documents-are-a-cache-not-a-ledger.md
"""

from __future__ import annotations

import time
from pathlib import Path

import pyright_session as pyright_session_module
import pytest
from pyright_session import PyrightSession
from repository_scope import resolve_repository_scope
from workspace_revision import compute_workspace_revision

from tests.code_kernel_helpers import create_semantic_pyright_fixture
from tests.slow_machine import SHORT_TIMEOUT


def _session(repository: Path, state_root: Path, fixture) -> PyrightSession:
    return PyrightSession(
        resolve_repository_scope(repository),
        fixture.identity,
        state_root=state_root,
    )


def _client_methods(fixture) -> list[tuple[str, str]]:
    return [
        (event["message"]["method"], event["message"]["params"]["textDocument"]["uri"])
        for event in fixture.events()
        if event["kind"] == "client-message"
        and "textDocument" in event["message"].get("params", {})
    ]


def _sources(repository: Path, count: int) -> list[str]:
    paths = []
    for index in range(count):
        name = f"pkg/room_{index}.py"
        (repository / name).write_bytes(b"value = %d\n" % index)
        paths.append(name)
    return paths


def test_the_file_after_the_limit_evicts_the_least_recently_used_one(
    repository: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pyright_session_module, "_MAX_OPEN_DOCUMENTS", 2)
    fixture = create_semantic_pyright_fixture(repository)
    session = _session(repository, state_root, fixture)
    names = _sources(repository, 3)
    try:
        first = session.open_document(names[0], deadline=time.monotonic() + SHORT_TIMEOUT)
        session.open_document(names[1], deadline=time.monotonic() + SHORT_TIMEOUT)
        session.open_document(names[0], deadline=time.monotonic() + SHORT_TIMEOUT)
        third = session.open_document(names[2], deadline=time.monotonic() + SHORT_TIMEOUT)
        closed = [
            uri
            for method, uri in _client_methods(fixture)
            if method == "textDocument/didClose"
        ]
        assert (closed, sorted(session._documents), len(session._documents)) == (
            [(repository / names[1]).resolve().as_uri()],
            sorted({first.source.uri, third.source.uri}),
            2,
        )
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_an_evicted_document_is_announced_again_when_it_is_asked_for(
    repository: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pyright_session_module, "_MAX_OPEN_DOCUMENTS", 1)
    fixture = create_semantic_pyright_fixture(repository)
    session = _session(repository, state_root, fixture)
    names = _sources(repository, 2)
    try:
        session.open_document(names[0], deadline=time.monotonic() + SHORT_TIMEOUT)
        session.open_document(names[1], deadline=time.monotonic() + SHORT_TIMEOUT)
        session.open_document(names[0], deadline=time.monotonic() + SHORT_TIMEOUT)
        opened = [
            uri
            for method, uri in _client_methods(fixture)
            if method == "textDocument/didOpen"
        ]
        uri = (repository / names[0]).resolve().as_uri()
        assert (opened.count(uri), session.readiness) == (2, "query_ready")
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


class _ReadCounter:
    """Count the confirming re-reads `synchronize` makes."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.reads = 0
        verified = pyright_session_module._verified_document_content
        counter = self

        def counted(entry: object, document: object, *, deadline: float) -> bytes:
            counter.reads += 1
            return verified(entry, document, deadline=deadline)

        monkeypatch.setattr(
            pyright_session_module, "_verified_document_content", counted
        )


def test_an_unmoved_open_document_is_not_read_again_by_the_next_query(
    repository: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = create_semantic_pyright_fixture(repository)
    session = _session(repository, state_root, fixture)
    scope = resolve_repository_scope(repository)
    names = _sources(repository, 2)
    counter = _ReadCounter(monkeypatch)
    try:
        for name in names:
            session.open_document(name, deadline=time.monotonic() + SHORT_TIMEOUT)
        revision = compute_workspace_revision(
            scope, deadline=time.monotonic() + SHORT_TIMEOUT
        )
        session.synchronize(revision, deadline=time.monotonic() + SHORT_TIMEOUT)
        first = counter.reads
        session.synchronize(revision, deadline=time.monotonic() + SHORT_TIMEOUT)
        assert (first, counter.reads) == (2, 2)
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_a_file_written_behind_the_session_is_still_caught(
    repository: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = create_semantic_pyright_fixture(repository)
    session = _session(repository, state_root, fixture)
    scope = resolve_repository_scope(repository)
    names = _sources(repository, 1)
    try:
        session.open_document(names[0], deadline=time.monotonic() + SHORT_TIMEOUT)
        revision = compute_workspace_revision(
            scope, deadline=time.monotonic() + SHORT_TIMEOUT
        )
        session.synchronize(revision, deadline=time.monotonic() + SHORT_TIMEOUT)
        (repository / names[0]).write_bytes(b"value = 'rewritten and longer'\n")
        with pytest.raises(RuntimeError, match="hash differs from the revision"):
            session.synchronize(revision, deadline=time.monotonic() + SHORT_TIMEOUT)
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)
