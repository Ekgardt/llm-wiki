from __future__ import annotations

import time
from pathlib import Path

import pytest
from llm_wiki.pyright_session_core import LanguageServerSession
from llm_wiki.pyright_session_values import (
    DiagnosticSnapshot,
    LspDiagnostic,
    OpenDocument,
    ProviderDiagnostics,
)
from lsp_positions import LspPosition, LspRange

from tests.language_server_session_state import (
    ScriptedProcess,
    document_of,
    make_query_ready,
    unstarted_session,
)
from tests.slow_machine import SHORT_TIMEOUT

_RANGE = {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 3}}


@pytest.fixture
def session(repository: Path, state_root: Path) -> LanguageServerSession:
    return unstarted_session(repository, state_root)


@pytest.fixture
def document(session: LanguageServerSession) -> OpenDocument:
    opened = document_of(session, "pkg/service.py")
    session._documents[opened.source.uri] = opened
    return opened


def _diagnostic(**fields: object) -> dict[str, object]:
    return {"range": _RANGE, "message": "broken", **fields}


def _publish(
    session: LanguageServerSession, document: OpenDocument, diagnostics: object, **extra: object
) -> DiagnosticSnapshot | None:
    session._publish_diagnostics({"uri": document.source.uri, "diagnostics": diagnostics, **extra})
    return session._diagnostics.get(document.source.uri)


def _expected(
    document: OpenDocument, severity: int | None = None, code: str | None = None
) -> LspDiagnostic:
    return LspDiagnostic(
        document.source.uri,
        LspRange(LspPosition(0, 0), LspPosition(0, 3)),
        severity,
        code,
        "broken",
        (),
    )


@pytest.mark.parametrize(
    "params",
    [
        "not an object",
        {"uri": 7, "diagnostics": []},
        {"uri": "file:///elsewhere.py", "diagnostics": "none"},
    ],
)
def test_an_unreadable_publication_is_ignored(
    session: LanguageServerSession, document: OpenDocument, params: object
) -> None:
    session._publish_diagnostics(params)

    assert session._diagnostics == {}


def test_a_publication_with_an_unreadable_version_is_ignored(
    session: LanguageServerSession, document: OpenDocument
) -> None:
    assert _publish(session, document, [_diagnostic()], version=-1) is None


def test_a_versionless_publication_is_kept_without_a_version(
    session: LanguageServerSession, document: OpenDocument
) -> None:
    snapshot = _publish(session, document, [_diagnostic(severity=2, code=17)])

    assert snapshot is not None
    assert (snapshot.diagnostics, snapshot.document_version, snapshot.partial) == (
        (_expected(document, severity=2, code="17"),),
        None,
        False,
    )


@pytest.mark.parametrize(
    "unreadable",
    [
        "not an object",
        _diagnostic(severity=True),
        _diagnostic(severity=9),
        _diagnostic(code=False),
        _diagnostic(code=2**31),
        _diagnostic(code=1.5),
        _diagnostic(message=None),
    ],
)
def test_an_unreadable_diagnostic_is_dropped_and_marks_the_snapshot_partial(
    session: LanguageServerSession, document: OpenDocument, unreadable: object
) -> None:
    snapshot = _publish(session, document, [unreadable, _diagnostic()], version=1)

    assert snapshot is not None
    assert (snapshot.diagnostics, snapshot.partial) == ((_expected(document),), True)


def test_related_information_keeps_readable_entries_and_reports_dropped_ones(
    session: LanguageServerSession, document: OpenDocument
) -> None:
    location = {"uri": document.source.uri, "range": _RANGE}
    related = [
        {"location": location},
        {"location": location, "message": "see here"},
        {"location": location, "message": 7},
        "not an object",
    ]

    snapshot = _publish(session, document, [_diagnostic(relatedInformation=related)], version=1)

    assert snapshot is not None
    entries = tuple((entry.uri, message) for entry, message in snapshot.diagnostics[0].related)
    assert entries == ((document.source.uri, None), (document.source.uri, "see here"))
    assert snapshot.partial is True


def test_related_information_that_is_not_a_list_marks_the_snapshot_partial(
    session: LanguageServerSession, document: OpenDocument
) -> None:
    snapshot = _publish(session, document, [_diagnostic(relatedInformation="x")], version=1)

    assert snapshot is not None
    assert (snapshot.diagnostics, snapshot.partial) == ((_expected(document),), True)


def test_a_versionless_publication_never_replaces_a_versioned_snapshot(
    session: LanguageServerSession, document: OpenDocument
) -> None:
    _publish(session, document, [_diagnostic()], version=2)

    kept = _publish(session, document, [], version=None)

    assert kept is not None
    assert (kept.document_version, len(kept.diagnostics)) == (2, 1)


def test_a_snapshot_for_a_document_that_closed_meanwhile_is_not_stored(
    session: LanguageServerSession, document: OpenDocument
) -> None:
    session._documents.clear()

    session._store_diagnostics(document.source.uri, 1, DiagnosticSnapshot((), 1, False, 128))

    assert session._diagnostics == {}


def test_an_expired_wait_answers_a_versionless_snapshot_as_partial(
    session: LanguageServerSession, document: OpenDocument
) -> None:
    make_query_ready(session, document, ScriptedProcess())
    held = _expected(document)
    session._diagnostics[document.source.uri] = DiagnosticSnapshot((held,), None, False, 128)

    answer = session._diagnostics_for_document(document, 0, time.monotonic() - 1.0)

    assert answer == ProviderDiagnostics((held,), None, True)


def test_diagnostics_without_a_server_are_an_empty_partial_answer(
    session: LanguageServerSession, document: OpenDocument
) -> None:
    answer = session._diagnostics_for_document(document, 0, time.monotonic() + SHORT_TIMEOUT)

    assert answer == ProviderDiagnostics((), None, True)


def test_diagnostics_during_synchronization_are_an_empty_partial_answer(
    session: LanguageServerSession,
) -> None:
    session._synchronize_epoch = 1

    answer = session.diagnostics("pkg/service.py", deadline=time.monotonic() + SHORT_TIMEOUT)

    assert answer == ProviderDiagnostics((), None, True)
