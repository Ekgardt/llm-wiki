from __future__ import annotations

import time
from pathlib import Path
from typing import cast

import pytest
from llm_wiki.pyright_session_core import LanguageServerSession
from llm_wiki.pyright_session_values import OpenDocument
from lsp_process import LspProcess
from lsp_security import resolve_repository_source
from repository_scope import resolve_repository_scope

from tests.pyright_session_helpers import _missing_identity


class _GenerationSwitchingProcess:
    def __init__(self, session: LanguageServerSession, next_generation: dict[str, str]) -> None:
        self.session = session
        self.next_generation = next_generation
        self.sent: list[str] = []

    def notify_generation(
        self, method: str, params: object, *, generation_nonce: str, deadline: float
    ) -> bool:
        self.sent.append(generation_nonce)
        following = self.next_generation[generation_nonce]
        self.session._generation_nonce = following
        self.session._begin_wire_generation(following)
        return False


class _RefusedNotify:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> bool:
        self.calls += 1
        raise RuntimeError("wire closed")


@pytest.fixture
def session(repository: Path, state_root: Path) -> LanguageServerSession:
    return LanguageServerSession(
        resolve_repository_scope(repository), _missing_identity(), state_root=state_root
    )


@pytest.fixture
def document(session: LanguageServerSession) -> OpenDocument:
    source = resolve_repository_source(session._repository, "pkg/service.py")
    content = source.absolute_path.read_bytes()
    return OpenDocument(source, content, "a" * 64, 1)


def _key(document: OpenDocument, generation: str) -> tuple[str, str, int]:
    return (generation, document.source.uri, document.version)


def _deadline() -> float:
    return time.monotonic() + 5.0


def test_discarding_the_current_wire_generation_forgets_its_opens(
    session: LanguageServerSession, document: OpenDocument
) -> None:
    session._begin_wire_generation("current")
    session._wire_opened.add(_key(document, "current"))
    session._wire_failed.add(_key(document, "current"))

    session._discard_wire_generation("current")

    assert (session._wire_generation, session._wire_opened, session._wire_failed) == (
        None,
        set(),
        set(),
    )


def test_discarding_another_wire_generation_keeps_the_current_one(
    session: LanguageServerSession, document: OpenDocument
) -> None:
    session._begin_wire_generation("current")
    session._wire_opened.add(_key(document, "current"))

    session._discard_wire_generation("retired")

    assert session._wire_generation == "current"
    assert session._wire_document_opened(document, "current") is True


def test_a_document_is_never_opened_on_no_generation(
    session: LanguageServerSession, document: OpenDocument
) -> None:
    assert session._wire_document_opened(document, None) is False


def test_a_failed_did_open_is_reported_and_never_resent_on_that_generation(
    session: LanguageServerSession, document: OpenDocument
) -> None:
    session._begin_wire_generation("current")
    notify = _RefusedNotify()

    with pytest.raises(RuntimeError, match="wire closed"):
        session._send_did_open_once(document, "current", deadline=_deadline(), notify=notify)
    again = session._send_did_open_once(document, "current", deadline=_deadline(), notify=notify)

    assert (again, notify.calls) == (False, 1)
    assert _key(document, "current") in session._wire_failed


def test_a_did_open_for_a_retired_generation_is_refused_without_sending(
    session: LanguageServerSession, document: OpenDocument
) -> None:
    session._begin_wire_generation("current")
    notify = _RefusedNotify()

    sent = session._send_did_open_once(document, "retired", deadline=_deadline(), notify=notify)

    assert (sent, notify.calls) == (False, 0)


def test_a_document_already_opened_is_answered_without_sending_again(
    session: LanguageServerSession, document: OpenDocument
) -> None:
    session._begin_wire_generation("current")
    session._wire_opened.add(_key(document, "current"))
    notify = _RefusedNotify()

    sent = session._send_did_open_once(document, "current", deadline=_deadline(), notify=notify)

    assert (sent, notify.calls) == (True, 0)


def test_waiting_on_another_sender_of_the_same_document_ends_at_the_deadline(
    session: LanguageServerSession, document: OpenDocument
) -> None:
    session._begin_wire_generation("current")
    session._wire_sending.add(_key(document, "current"))

    with pytest.raises(TimeoutError, match="didOpen send gate deadline expired"):
        session._send_did_open_once(
            document, "current", deadline=time.monotonic() - 1.0, notify=_RefusedNotify()
        )


def test_a_session_without_a_generation_opens_nothing_on_the_process(
    session: LanguageServerSession, document: OpenDocument
) -> None:
    process = _GenerationSwitchingProcess(session, {})

    sent = session._send_process_did_open(document, cast(LspProcess, process), deadline=_deadline())

    assert (sent, process.sent) == (False, [])


def test_a_refused_did_open_follows_a_new_generation_but_never_retries_an_old_one(
    session: LanguageServerSession, document: OpenDocument
) -> None:
    session._generation_nonce = "first"
    session._begin_wire_generation("first")
    process = _GenerationSwitchingProcess(session, {"first": "second", "second": "first"})

    sent = session._send_process_did_open(document, cast(LspProcess, process), deadline=_deadline())

    assert (sent, process.sent) == (False, ["first", "second"])
