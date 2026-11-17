from __future__ import annotations

import time
from pathlib import Path
from typing import cast

import pytest
from code_intelligence import PositionEncoding
from llm_wiki.pyright_session_core import LanguageServerSession
from llm_wiki.pyright_session_queries import DocumentQuery, _hover_response
from llm_wiki.pyright_session_values import (
    LspLocation,
    OpenDocument,
    ProviderHover,
    ProviderLocations,
)
from lsp_positions import LspPosition, LspRange, SourceAnchor
from lsp_profiles import TYPESCRIPT_PROFILE
from lsp_protocol import MAX_HOVER_BYTES, MAX_LOCATIONS
from repository_scope import resolve_repository_scope

from tests.language_server_session_state import (
    READY_GENERATION,
    ScriptedProcess,
    document_of,
    make_query_ready,
    retire_generation,
    unstarted_session,
)
from tests.pyright_session_helpers import _missing_identity
from tests.slow_machine import SHORT_TIMEOUT

_RANGE = {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 3}}
_WIDE = {"start": {"line": 0, "character": 0}, "end": {"line": 4, "character": 0}}
_LSP_RANGE = LspRange(LspPosition(0, 0), LspPosition(0, 3))


@pytest.fixture
def session(repository: Path, state_root: Path) -> LanguageServerSession:
    return unstarted_session(repository, state_root)


@pytest.fixture
def document(session: LanguageServerSession) -> OpenDocument:
    return document_of(session, "pkg/service.py")


def _deadline() -> float:
    return time.monotonic() + SHORT_TIMEOUT


def _symbol(**fields: object) -> dict[str, object]:
    return {"name": "run", "kind": 12, "range": _WIDE, "selectionRange": _RANGE, **fields}


def _bound_query(
    session: LanguageServerSession, document: OpenDocument, process: ScriptedProcess
) -> DocumentQuery:
    live = make_query_ready(session, document, process)
    epoch = session._semantic_query_epoch()
    assert epoch is not None
    return DocumentQuery(live, READY_GENERATION, document, epoch, PositionEncoding.UTF16)


def test_no_hover_is_a_complete_empty_answer() -> None:
    assert _hover_response(None) == ProviderHover(None, None, False)


@pytest.mark.parametrize("result", ["text", {"range": _RANGE}])
def test_a_hover_without_contents_is_partial(result: object) -> None:
    assert _hover_response(result) == ProviderHover(None, None, True)


@pytest.mark.parametrize(
    "contents",
    [
        "\ud800",
        "x" * (MAX_HOVER_BYTES + 1),
        {"kind": "other", "value": "text"},
        {"language": "", "value": "text"},
        7,
        [7],
        ["x"] * 1025,
        ["x" * (MAX_HOVER_BYTES // 2), "y" * (MAX_HOVER_BYTES // 2)],
    ],
)
def test_unusable_hover_contents_are_dropped_as_partial(contents: object) -> None:
    answer = _hover_response({"contents": contents, "range": _RANGE})

    assert (answer.contents, answer.partial) == (None, True)


def test_a_labelled_fragment_is_kept_beside_a_dropped_one() -> None:
    contents = [{"language": "python", "value": "def run()"}, 7]

    assert _hover_response({"contents": contents, "range": _RANGE}) == ProviderHover(
        "def run()", _LSP_RANGE, True
    )


def test_a_hover_without_a_range_is_complete_without_one() -> None:
    assert _hover_response({"contents": "text"}) == ProviderHover("text", None, False)


def test_a_hover_for_something_that_is_not_an_anchor_is_refused(
    session: LanguageServerSession,
) -> None:
    with pytest.raises(TypeError, match="anchor must be a SourceAnchor"):
        session.hover(cast(SourceAnchor, "x"), deadline=_deadline())


def test_a_definition_for_something_that_is_not_an_anchor_is_refused(
    session: LanguageServerSession,
) -> None:
    with pytest.raises(TypeError, match="anchor must be a SourceAnchor"):
        session.definition(cast(SourceAnchor, "x"), deadline=_deadline())


def test_a_definition_past_its_deadline_is_refused(session: LanguageServerSession) -> None:
    anchor = SourceAnchor("pkg/service.py", 1, 0, 0)

    with pytest.raises(TimeoutError, match="semantic request deadline expired"):
        session.definition(anchor, deadline=time.monotonic() - 1.0)


def test_a_workspace_symbol_query_that_is_not_text_is_refused(
    session: LanguageServerSession,
) -> None:
    with pytest.raises(TypeError, match="query must be a string"):
        session.workspace_symbols(cast(str, 5), deadline=_deadline())


def test_an_oversized_workspace_symbol_query_is_refused(session: LanguageServerSession) -> None:
    with pytest.raises(ValueError, match="query exceeds 4096 bytes"):
        session.workspace_symbols("q" * 4097, deadline=_deadline())


@pytest.mark.parametrize(("result", "partial"), [(None, False), ("x", True)])
def test_a_workspace_symbol_answer_that_is_not_a_list_holds_no_locations(
    session: LanguageServerSession, result: object, partial: bool
) -> None:
    answer = session._workspace_symbol_response(result)

    assert answer == ProviderLocations((), "provider_reported", partial)


def test_workspace_symbols_without_a_location_are_dropped_as_partial(
    session: LanguageServerSession, document: OpenDocument
) -> None:
    located = {"name": "run", "location": {"uri": document.source.uri, "range": _RANGE}}

    answer = session._workspace_symbol_response(["x", {"name": "run"}, located])

    assert answer == ProviderLocations(
        (LspLocation(document.source.uri, _LSP_RANGE),), "provider_reported", True
    )


@pytest.mark.parametrize(
    "value",
    [
        "x",
        {"targetUri": "file:///x.py", "targetRange": "bad"},
        {"uri": 7, "range": _RANGE},
    ],
)
def test_an_unreadable_location_is_dropped_as_partial(
    session: LanguageServerSession, value: object
) -> None:
    assert session._normalize_locations([value]) == ((), True)


@pytest.mark.parametrize(("result", "partial"), [(None, False), ("x", True)])
def test_document_symbols_that_are_not_a_list_hold_no_locations(
    session: LanguageServerSession, document: OpenDocument, result: object, partial: bool
) -> None:
    assert session._normalize_document_symbols(result, document.source.uri) == ((), partial)


@pytest.mark.parametrize(
    "unreadable",
    [
        _symbol(children="x"),
        _symbol(deprecated="yes"),
        _symbol(tags="x"),
        _symbol(detail=7),
        _symbol(selectionRange=None),
        _symbol(range=_RANGE, selectionRange=_WIDE),
        _symbol(location="x"),
    ],
)
def test_an_unreadable_document_symbol_marks_the_answer_partial(
    session: LanguageServerSession, document: OpenDocument, unreadable: object
) -> None:
    _locations, partial = session._normalize_document_symbols([unreadable], document.source.uri)

    assert partial is True


def test_a_well_formed_document_symbol_with_flags_and_detail_is_kept(
    session: LanguageServerSession, document: OpenDocument
) -> None:
    symbol = _symbol(deprecated=False, tags=[1], detail="def run()")

    answer = session._normalize_document_symbols([symbol], document.source.uri)

    assert answer == ((LspLocation(document.source.uri, _LSP_RANGE),), False)


def test_symbols_naming_the_same_location_are_answered_once(
    session: LanguageServerSession, document: OpenDocument
) -> None:
    location = {"uri": document.source.uri, "range": _RANGE}
    symbols = [_symbol(location=location), _symbol(location=location)]

    answer = session._normalize_document_symbols(symbols, document.source.uri)

    assert answer == ((LspLocation(document.source.uri, _LSP_RANGE),), False)


def test_a_symbol_node_seen_twice_is_visited_once(
    session: LanguageServerSession, document: OpenDocument
) -> None:
    shared = _symbol()

    locations, partial = session._normalize_document_symbols([shared, shared], document.source.uri)

    assert (len(locations), partial) == (1, True)


def test_children_beyond_the_location_bound_are_dropped_as_partial(
    session: LanguageServerSession, document: OpenDocument
) -> None:
    parent = _symbol(children=[_symbol() for _ in range(MAX_LOCATIONS + 1)])

    locations, partial = session._normalize_document_symbols([parent], document.source.uri)

    assert (len(locations), partial) == (1, True)


def test_a_hover_whose_generation_retired_before_asking_sends_nothing(
    session: LanguageServerSession, document: OpenDocument
) -> None:
    process = ScriptedProcess()
    query = _bound_query(session, document, process)
    retire_generation(session)

    answer = session._hover_response(query, LspPosition(0, 0), deadline=_deadline())

    assert (answer, process.requests) == (ProviderHover(None, None, True), [])


def test_a_location_query_whose_generation_retired_before_asking_sends_nothing(
    session: LanguageServerSession, document: OpenDocument
) -> None:
    process = ScriptedProcess()
    query = _bound_query(session, document, process)
    retire_generation(session)

    answer = session._location_response(
        query, "textDocument/definition", {}, deadline=_deadline(), references=False
    )

    assert (answer, process.requests) == (ProviderLocations((), "not_ready", True), [])


def test_a_document_query_without_a_server_is_not_bound(
    session: LanguageServerSession, document: OpenDocument
) -> None:
    assert session._document_query(document, 0) is None


def test_a_progress_gated_session_recomputes_readiness_after_the_wait(
    repository: Path, state_root: Path
) -> None:
    gated = LanguageServerSession(
        resolve_repository_scope(repository),
        _missing_identity(),
        state_root=state_root,
        profile=TYPESCRIPT_PROFILE,
    )
    stale: tuple[str, ...] = ("stale",)
    gated._readiness = "query_ready"
    gated._readiness_evidence = stale

    gated._refresh_readiness_after_progress(_deadline())

    assert (gated._readiness, gated._readiness_evidence) == ("not_ready", ())
