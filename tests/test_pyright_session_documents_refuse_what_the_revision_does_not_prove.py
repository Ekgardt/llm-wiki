from __future__ import annotations

import time
from pathlib import Path
from typing import cast

import pytest
from llm_wiki import pyright_session_documents
from llm_wiki.pyright_session_core import LanguageServerSession
from llm_wiki.pyright_session_documents import (
    _check_changed_entry,
    _check_did_open_encodable,
    _check_document_unchanged,
    _file_identity,
    _retained_document_bytes,
    _SyncSnapshot,
    _validated_entry_size,
)
from llm_wiki.pyright_session_values import OpenDocument
from lsp_positions import path_to_file_uri
from lsp_process import LspProcess
from workspace_revision import RevisionEntry, WorkspaceDelta

from tests.language_server_session_state import (
    READY_GENERATION,
    document_of,
    unstarted_session,
)
from tests.slow_machine import SHORT_TIMEOUT


class _RefusingProcess:
    generation_nonce = READY_GENERATION

    def __init__(self) -> None:
        self.refused: list[str] = []

    def notify_generation(
        self, method: str, params: object, *, generation_nonce: str, deadline: float
    ) -> bool:
        self.refused.append(method)
        raise RuntimeError(f"{method} refused")


@pytest.fixture
def session(repository: Path, state_root: Path) -> LanguageServerSession:
    return unstarted_session(repository, state_root)


@pytest.fixture
def document(session: LanguageServerSession) -> OpenDocument:
    return document_of(session, "pkg/service.py")


def _deadline() -> float:
    return time.monotonic() + SHORT_TIMEOUT


def _entry(document: OpenDocument, size: object) -> RevisionEntry:
    return RevisionEntry(
        document.source.relative_path, "file", document.source_sha256, cast(int, size)
    )


def _refusing_session(session: LanguageServerSession) -> _RefusingProcess:
    process = _RefusingProcess()
    session._process = cast(LspProcess, process)
    session._generation_nonce = READY_GENERATION
    session._begin_wire_generation(READY_GENERATION)
    return process


@pytest.mark.parametrize("size", [True, "12"])
def test_an_entry_size_that_is_not_an_integer_is_refused(
    document: OpenDocument, size: object
) -> None:
    with pytest.raises(RuntimeError, match="byte limit exceeded"):
        _validated_entry_size(_entry(document, size))


def test_a_file_that_is_gone_has_no_identity(tmp_path: Path) -> None:
    assert _file_identity(tmp_path / "gone.py") is None


def test_an_open_document_absent_from_the_revision_is_refused(document: OpenDocument) -> None:
    with pytest.raises(RuntimeError, match="absent from the workspace revision"):
        _retained_document_bytes(None, document)


def test_an_open_document_whose_size_differs_from_the_revision_is_refused(
    document: OpenDocument,
) -> None:
    with pytest.raises(RuntimeError, match="size differs from the revision"):
        _retained_document_bytes(_entry(document, len(document.content) + 1), document)


def test_a_changed_document_the_revision_does_not_describe_is_refused(
    document: OpenDocument,
) -> None:
    with pytest.raises(RuntimeError, match="changed source document hash differs"):
        _check_changed_entry(None, document.source_sha256, document.content)


def test_an_open_document_that_changed_unsynchronized_is_refused(
    document: OpenDocument,
) -> None:
    with pytest.raises(RuntimeError, match="changed without synchronization"):
        _check_document_unchanged(document, document.source, b"other", document.source_sha256)


def test_a_document_the_wire_could_not_carry_is_refused() -> None:
    with pytest.raises(ValueError, match="exceeds the LSP frame"):
        _check_did_open_encodable({"textDocument": {"text": "\ud800"}})


def test_a_refused_did_open_is_answered_as_not_opened(
    session: LanguageServerSession, document: OpenDocument
) -> None:
    process = _refusing_session(session)

    opened = session._try_process_did_open(document, cast(LspProcess, process), _deadline())

    assert (opened, process.refused) == (False, ["textDocument/didOpen"])


def test_reopening_a_held_document_the_server_refused_keeps_the_held_one(
    session: LanguageServerSession, document: OpenDocument
) -> None:
    process = _refusing_session(session)

    kept = session._reopen_existing_document(
        document, cast(LspProcess, process), ready=False, deadline=_deadline()
    )

    assert (kept, session._ready_uri_generations) == (document, {})


def test_opening_beyond_the_document_count_bound_is_refused(
    session: LanguageServerSession, document: OpenDocument, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pyright_session_documents, "MAX_OPEN_DOCUMENTS", 1)
    held = document_of(session, "pkg/api.py")
    session._documents[held.source.uri] = held

    with session._lock, pytest.raises(RuntimeError, match="document count limit exceeded"):
        session._register_document_locked(document, document.content)


def test_forgetting_a_uri_without_a_wire_generation_changes_nothing(
    session: LanguageServerSession, document: OpenDocument
) -> None:
    key = ("generation-old", document.source.uri, 1)
    session._wire_opened.add(key)

    session._forget_wire_uri_locked(document.source.uri)

    assert session._wire_opened == {key}


def test_forgetting_a_document_that_was_replaced_keeps_the_replacement(
    session: LanguageServerSession, document: OpenDocument
) -> None:
    replacement = document_of(session, "pkg/service.py", version=2)
    session._documents[document.source.uri] = replacement

    session._forget_document_locked(document)

    assert session._documents == {document.source.uri: replacement}


def test_forgetting_a_document_that_is_not_the_readiness_target_keeps_the_target(
    session: LanguageServerSession, document: OpenDocument
) -> None:
    target = document_of(session, "pkg/api.py")
    session._documents[document.source.uri] = document
    session._document_bytes = len(document.content)
    session._readiness_target_uri = target.source.uri

    session._forget_document_locked(document)

    assert (session._documents, session._readiness_target_uri) == ({}, target.source.uri)


def test_a_refused_did_close_never_fails_the_caller() -> None:
    process = _RefusingProcess()

    LanguageServerSession._try_process_did_close(
        cast(LspProcess, process), {}, READY_GENERATION, _deadline()
    )

    assert process.refused == ["textDocument/didClose"]


def test_opening_a_document_without_a_server_is_refused(
    session: LanguageServerSession,
) -> None:
    with pytest.raises(RuntimeError, match="not protocol initialized"):
        session.open_document("pkg/service.py", deadline=_deadline())


def test_opening_a_document_past_its_deadline_is_refused(
    session: LanguageServerSession,
) -> None:
    with pytest.raises(TimeoutError, match="document deadline expired"):
        session.open_document("pkg/service.py", deadline=time.monotonic() - 1.0)


def test_synchronizing_without_a_server_is_refused(session: LanguageServerSession) -> None:
    with pytest.raises(RuntimeError, match="not protocol initialized"):
        session._synchronize_snapshot()


def test_a_file_identity_is_not_kept_when_none_was_seen_before_the_read(
    session: LanguageServerSession, document: OpenDocument
) -> None:
    session._record_document_identity(document.source.uri, document.source.absolute_path, None)

    assert session._document_identities == {}


def test_a_changed_file_that_is_not_open_is_announced_as_a_watched_change(
    session: LanguageServerSession,
) -> None:
    snapshot = _SyncSnapshot(cast(LspProcess, _RefusingProcess()), READY_GENERATION, None, {}, {})
    delta = WorkspaceDelta((), ("pkg/api.py",), (), ())

    watched, replacements = session._planned_changes(snapshot, delta, {}, {}, deadline=_deadline())

    uri = path_to_file_uri(Path(session._repository.checkout_root, "pkg/api.py"))
    assert (watched, replacements) == ([{"uri": uri, "type": 2}], [])
