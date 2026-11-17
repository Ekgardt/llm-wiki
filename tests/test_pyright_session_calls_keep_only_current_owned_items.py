from __future__ import annotations

import time
from pathlib import Path
from typing import cast

import pytest
from code_intelligence import PositionEncoding
from llm_wiki.pyright_session_core import LanguageServerSession
from llm_wiki.pyright_session_queries import DocumentQuery
from llm_wiki.pyright_session_values import LspLocation, OpenDocument, ProviderCalls
from lsp_positions import LspPosition, LspRange, SourceAnchor

from tests.language_server_session_state import (
    READY_GENERATION,
    ScriptedProcess,
    document_of,
    make_query_ready,
    retire_generation,
    unstarted_session,
)
from tests.slow_machine import SHORT_TIMEOUT

_RANGE = {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 3}}
_WIDE = {"start": {"line": 0, "character": 0}, "end": {"line": 4, "character": 0}}
_PREPARE = "textDocument/prepareCallHierarchy"
_INCOMING = "callHierarchy/incomingCalls"
_ANCHOR = SourceAnchor("pkg/service.py", 1, 0, 0)


@pytest.fixture
def session(repository: Path, state_root: Path) -> LanguageServerSession:
    return unstarted_session(repository, state_root)


@pytest.fixture
def document(session: LanguageServerSession) -> OpenDocument:
    return document_of(session, "pkg/service.py")


def _deadline() -> float:
    return time.monotonic() + SHORT_TIMEOUT


def _item(document: OpenDocument, **fields: object) -> dict[str, object]:
    return {
        "name": "run",
        "kind": 12,
        "uri": document.source.uri,
        "range": _WIDE,
        "selectionRange": _RANGE,
        **fields,
    }


def _bound_query(
    session: LanguageServerSession, document: OpenDocument, process: ScriptedProcess
) -> DocumentQuery:
    live = make_query_ready(session, document, process)
    epoch = session._semantic_query_epoch()
    assert epoch is not None
    return DocumentQuery(live, READY_GENERATION, document, epoch, PositionEncoding.UTF16)


def _retire_on(session: LanguageServerSession, method: str) -> ScriptedProcess:
    def retire(requested: str) -> None:
        if requested == method:
            retire_generation(session)

    return ScriptedProcess(on_request=retire)


@pytest.mark.parametrize(
    "unusable",
    [
        {"selectionRange": None},
        {"range": _RANGE, "selectionRange": _WIDE},
        {"uri": "file:///elsewhere/outside.py"},
        {"detail": 7},
        {"tags": "x"},
    ],
)
def test_an_unusable_call_item_is_dropped(
    session: LanguageServerSession, document: OpenDocument, unusable: dict[str, object]
) -> None:
    assert session._sanitize_call_item(_item(document, **unusable)) is None


def test_a_call_item_that_is_not_an_object_is_dropped(session: LanguageServerSession) -> None:
    assert session._sanitize_call_item("x") is None


def test_a_call_item_keeps_its_detail_tags_and_data(
    session: LanguageServerSession, document: OpenDocument
) -> None:
    value = _item(document, detail="def run()", tags=[1], data={"opaque": True})

    item = session._sanitize_call_item(value)

    assert item is not None
    assert (item["detail"], item["tags"], item["data"]) == ("def run()", [1], {"opaque": True})


def test_calls_keep_the_owned_locations_and_skip_unreadable_entries(
    session: LanguageServerSession, document: OpenDocument
) -> None:
    item = _item(document)
    calls = [{"from": item}, "x", {"from": "x"}]
    process = ScriptedProcess({_PREPARE: [item, item], _INCOMING: calls})
    query = _bound_query(session, document, process)

    answer = session._collect_calls(_ANCHOR, query, direction="incoming", deadline=_deadline())

    location = LspLocation(document.source.uri, LspRange(LspPosition(0, 0), LspPosition(0, 3)))
    assert answer == ProviderCalls("incoming", (location,), "provider_reported", True)
    assert process.requested_methods() == [_PREPARE, _INCOMING, _INCOMING]


def test_a_call_result_that_is_not_a_list_adds_nothing(
    session: LanguageServerSession, document: OpenDocument
) -> None:
    process = ScriptedProcess({_PREPARE: [_item(document)], _INCOMING: "x"})
    query = _bound_query(session, document, process)

    answer = session._collect_calls(_ANCHOR, query, direction="incoming", deadline=_deadline())

    assert answer == ProviderCalls("incoming", (), "provider_reported", True)


def test_a_prepared_answer_that_is_not_a_list_is_partial(
    session: LanguageServerSession, document: OpenDocument
) -> None:
    query = _bound_query(session, document, ScriptedProcess({_PREPARE: "x"}))

    answer = session._collect_calls(_ANCHOR, query, direction="incoming", deadline=_deadline())

    assert answer == ProviderCalls("incoming", (), "provider_reported", True)


def test_a_call_hierarchy_whose_generation_retired_first_sends_nothing(
    session: LanguageServerSession, document: OpenDocument
) -> None:
    process = ScriptedProcess()
    query = _bound_query(session, document, process)
    retire_generation(session)

    answer = session._collect_calls(_ANCHOR, query, direction="incoming", deadline=_deadline())

    assert (answer, process.requests) == (ProviderCalls("incoming", (), "not_ready", True), [])


def test_calls_whose_generation_retires_during_a_request_are_not_ready(
    session: LanguageServerSession, document: OpenDocument
) -> None:
    process = _retire_on(session, _INCOMING)
    process.answers[_PREPARE] = [_item(document), _item(document)]
    query = _bound_query(session, document, process)

    answer = session._collect_calls(_ANCHOR, query, direction="incoming", deadline=_deadline())

    assert answer == ProviderCalls("incoming", (), "not_ready", True)
    assert process.requested_methods() == [_PREPARE, _INCOMING]


def test_a_call_request_on_a_retired_generation_is_never_sent(
    session: LanguageServerSession, document: OpenDocument
) -> None:
    process = ScriptedProcess()
    query = _bound_query(session, document, process)
    retire_generation(session)

    assert session._call_result({}, query, method=_INCOMING, deadline=_deadline()) == (
        False,
        None,
    )
    assert process.requests == []


def test_calls_with_no_items_on_a_retired_generation_are_not_ready(
    session: LanguageServerSession, document: OpenDocument
) -> None:
    query = _bound_query(session, document, ScriptedProcess())
    retire_generation(session)

    assert session._call_locations([], query, direction="incoming", deadline=_deadline()) is None


def test_calls_for_a_document_that_cannot_be_bound_are_not_ready(
    session: LanguageServerSession, document: OpenDocument, monkeypatch: pytest.MonkeyPatch
) -> None:
    def opened(path: str, *, deadline: float) -> OpenDocument:
        return document

    monkeypatch.setattr(session, "open_document", opened)

    answer = session._calls_for_open_document(
        _ANCHOR, 0, direction="outgoing", deadline=_deadline()
    )

    assert answer == ProviderCalls("outgoing", (), "not_ready", True)


def test_calls_for_something_that_is_not_an_anchor_are_refused(
    session: LanguageServerSession,
) -> None:
    with pytest.raises(TypeError, match="anchor must be a SourceAnchor"):
        session.incoming_calls(cast(SourceAnchor, "x"), deadline=_deadline())
