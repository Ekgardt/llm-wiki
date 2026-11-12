"""Semantic queries of the pinned Pyright session: capability honesty and normalized answers."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from llm_wiki.pyright_session_values import (
    LspLocation,
    ProviderCalls,
    ProviderHover,
    ProviderLocations,
)
from lsp_positions import LspPosition, LspRange, SourceAnchor

from tests.code_kernel_helpers import (
    SemanticPyrightFixture,
    create_semantic_pyright_fixture,
)
from tests.pyright_session_helpers import (
    _anchor,
    _client_methods,
    _session,
    semantic_pyright,  # noqa: F401
)
from tests.slow_machine import SHORT_TIMEOUT


def test_definition_references_type_and_implementation_are_capability_honest(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    anchor = _anchor(repository, "pkg/service.py", 10, 20)
    try:
        definition = session.definition(anchor, deadline=time.monotonic() + SHORT_TIMEOUT)
        references = session.references(anchor, deadline=time.monotonic() + SHORT_TIMEOUT)
        type_definition = session.type_definition(
            anchor,
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        implementations = session.implementations(
            anchor,
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )

        assert definition == ProviderLocations(
            (
                LspLocation(
                    (repository / "pkg/api.py").resolve().as_uri(),
                    LspRange(LspPosition(1, 8), LspPosition(1, 14)),
                ),
            ),
            "provider_reported",
            False,
        )
        assert len(references.locations) == 2
        assert references.coverage == "provider_reported"
        assert references.partial is True
        assert type_definition == ProviderLocations(
            (
                LspLocation(
                    (repository / "pkg/api.py").resolve().as_uri(),
                    LspRange(LspPosition(0, 6), LspPosition(0, 15)),
                ),
            ),
            "provider_reported",
            False,
        )
        assert implementations == ProviderLocations((), "unsupported", True)
        methods = [
            event["method"]
            for event in semantic_pyright.events()
            if event["kind"] == "client-message"
        ]
        assert "textDocument/implementation" not in methods
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


@pytest.mark.parametrize("advertised", ["missing", "false"])
def test_missing_or_false_capability_sends_no_feature_request(
    repository: Path,
    state_root: Path,
    advertised: str,
) -> None:
    capabilities: dict[str, object] = {"documentSymbolProvider": True}
    if advertised == "false":
        capabilities["definitionProvider"] = False
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"capabilities": capabilities},
    )
    session = _session(repository, state_root, fixture)
    try:
        result = session.definition(
            _anchor(repository, "pkg/service.py", 10, 20),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert result == ProviderLocations((), "unsupported", True)
        assert "textDocument/definition" not in _client_methods(fixture)
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_not_ready_never_returns_a_complete_provider_negative(
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"document_symbol_failures": 10},
    )
    session = _session(repository, state_root, fixture)
    try:
        result = session.definition(
            _anchor(repository, "pkg/service.py", 10, 20),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert result == ProviderLocations((), "not_ready", True)
        assert "textDocument/definition" not in _client_methods(fixture)
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_unsupported_document_features_return_before_source_open(
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"capabilities": {}},
    )
    session = _session(repository, state_root, fixture)
    anchor = SourceAnchor("missing.py", 1, 0, 0)
    try:
        assert session.definition(
            anchor,
            deadline=time.monotonic() + SHORT_TIMEOUT,
        ) == ProviderLocations((), "unsupported", True)
        assert session.hover(
            anchor,
            deadline=time.monotonic() + SHORT_TIMEOUT,
        ) == ProviderHover(None, None, True)
        assert session.incoming_calls(
            anchor,
            deadline=time.monotonic() + SHORT_TIMEOUT,
        ) == ProviderCalls("incoming", (), "unsupported", True)
        assert session.document_symbols(
            "missing.py",
            deadline=time.monotonic() + SHORT_TIMEOUT,
        ) == ProviderLocations((), "unsupported", True)
        methods = [
            event["method"]
            for event in fixture.events()
            if event["kind"] == "client-message"
        ]
        assert "textDocument/didOpen" not in methods
        assert "textDocument/documentSymbol" not in methods
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_unsupported_workspace_symbols_precede_query_readiness(
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"capabilities": {"documentSymbolProvider": True}},
    )
    session = _session(repository, state_root, fixture)
    try:
        assert session.workspace_symbols(
            "Service",
            deadline=time.monotonic() + SHORT_TIMEOUT,
        ) == ProviderLocations((), "unsupported", True)
        methods = [
            event["method"]
            for event in fixture.events()
            if event["kind"] == "client-message"
        ]
        assert "workspace/symbol" not in methods
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_locations_filter_external_malformed_and_duplicate_entries_in_wire_order(
    repository: Path,
    state_root: Path,
) -> None:
    valid = {
        "uri": "$API_URI",
        "range": {
            "start": {"line": 1, "character": 8},
            "end": {"line": 1, "character": 14},
        },
    }
    linked = {
        "targetUri": "$SERVICE_URI",
        "targetRange": {
            "start": {"line": 4, "character": 0},
            "end": {"line": 4, "character": 20},
        },
        "targetSelectionRange": {
            "start": {"line": 4, "character": 6},
            "end": {"line": 4, "character": 13},
        },
    }
    fixture = create_semantic_pyright_fixture(
        repository,
        config={
            "responses": {
                "textDocument/definition": [
                    valid,
                    valid,
                    {
                        "uri": "$EXTERNAL_URI",
                        "range": valid["range"],
                    },
                    {
                        "uri": "$API_URI",
                        "range": {
                            "start": {"line": True, "character": 0},
                            "end": {"line": 1, "character": 0},
                        },
                    },
                    linked,
                    {"uri": "$API_URI"},
                ]
            }
        },
    )
    session = _session(repository, state_root, fixture)
    try:
        result = session.definition(
            _anchor(repository, "pkg/service.py", 10, 20),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert result == ProviderLocations(
            (
                LspLocation(
                    (repository / "pkg/api.py").resolve().as_uri(),
                    LspRange(LspPosition(1, 8), LspPosition(1, 14)),
                ),
                LspLocation(
                    (repository / "pkg/service.py").resolve().as_uri(),
                    LspRange(LspPosition(4, 6), LspPosition(4, 13)),
                ),
            ),
            "provider_reported",
            True,
        )
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_anchor_is_validated_against_current_document_and_negotiated_encoding(
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={
            "capabilities": {
                "definitionProvider": True,
                "documentSymbolProvider": True,
                "positionEncoding": "utf-16",
            }
        },
    )
    session = _session(repository, state_root, fixture)
    anchor = _anchor(repository, "pkg/unicode_api.py", 1, len('VALUE = "a😀'.encode()))
    try:
        session.definition(anchor, deadline=time.monotonic() + SHORT_TIMEOUT)
        definition = next(
            event["message"]
            for event in fixture.events()
            if event.get("method") == "textDocument/definition"
        )
        assert definition["params"]["position"] == {"line": 0, "character": 12}

        invalid = SourceAnchor(
            "pkg/unicode_api.py",
            anchor.line,
            anchor.utf8_character,
            0,
        )
        with pytest.raises(ValueError, match="byte_offset"):
            session.definition(invalid, deadline=time.monotonic() + SHORT_TIMEOUT)
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_document_and_workspace_symbols_normalize_selection_ranges(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        document = session.document_symbols(
            "pkg/service.py",
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        workspace = session.workspace_symbols(
            "Service",
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        service_uri = (repository / "pkg/service.py").resolve().as_uri()
        assert document == ProviderLocations(
            (
                LspLocation(
                    service_uri,
                    LspRange(LspPosition(4, 6), LspPosition(4, 13)),
                ),
                LspLocation(
                    service_uri,
                    LspRange(LspPosition(8, 8), LspPosition(8, 15)),
                ),
            ),
            "provider_reported",
            False,
        )
        assert workspace == ProviderLocations(
            (
                LspLocation(
                    service_uri,
                    LspRange(LspPosition(4, 6), LspPosition(4, 13)),
                ),
                LspLocation(
                    (repository / "pkg/api.py").resolve().as_uri(),
                    LspRange(LspPosition(0, 6), LspPosition(0, 15)),
                ),
            ),
            "provider_reported",
            False,
        )
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


@pytest.mark.parametrize(
    ("contents", "expected"),
    [
        ("plain hover", "plain hover"),
        ({"kind": "markdown", "value": "**hover**"}, "**hover**"),
        ({"language": "python", "value": "def hover(): ..."}, "def hover(): ..."),
        (
            [
                "first",
                {"kind": "plaintext", "value": "second"},
                {"language": "python", "value": "third"},
            ],
            "first\n\nsecond\n\nthird",
        ),
    ],
)
def test_hover_normalizes_string_markup_and_marked_string_forms(
    repository: Path,
    state_root: Path,
    contents: object,
    expected: str,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={
            "responses": {
                "textDocument/hover": {
                    "contents": contents,
                    "range": {
                        "start": {"line": 8, "character": 8},
                        "end": {"line": 8, "character": 15},
                    },
                }
            }
        },
    )
    session = _session(repository, state_root, fixture)
    try:
        hover = session.hover(
            _anchor(repository, "pkg/service.py", 10, 20),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert hover == ProviderHover(
            expected,
            LspRange(LspPosition(8, 8), LspPosition(8, 15)),
            False,
        )
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_malformed_hover_is_bounded_partial_instead_of_complete_negative(
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={
            "responses": {
                "textDocument/hover": {
                    "contents": [{"kind": "plaintext", "value": 3}],
                    "range": {
                        "start": {"line": -1, "character": 0},
                        "end": {"line": 0, "character": 0},
                    },
                }
            }
        },
    )
    session = _session(repository, state_root, fixture)
    try:
        assert session.hover(
            _anchor(repository, "pkg/service.py", 10, 20),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        ) == ProviderHover(None, None, True)
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_incoming_and_outgoing_calls_use_call_hierarchy_not_references(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    anchor = _anchor(repository, "pkg/service.py", 10, 20)
    try:
        incoming = session.incoming_calls(anchor, deadline=time.monotonic() + SHORT_TIMEOUT)
        outgoing = session.outgoing_calls(anchor, deadline=time.monotonic() + SHORT_TIMEOUT)
        assert incoming == ProviderCalls(
            "incoming",
            (
                LspLocation(
                    (repository / "pkg/service.py").resolve().as_uri(),
                    LspRange(LspPosition(12, 4), LspPosition(12, 11)),
                ),
            ),
            "provider_reported",
            True,
        )
        assert outgoing == ProviderCalls(
            "outgoing",
            (
                LspLocation(
                    (repository / "pkg/api.py").resolve().as_uri(),
                    LspRange(LspPosition(1, 4), LspPosition(1, 11)),
                ),
            ),
            "provider_reported",
            True,
        )
        methods = [
            event["method"]
            for event in semantic_pyright.events()
            if event["kind"] == "client-message"
        ]
        assert methods.count("textDocument/prepareCallHierarchy") == 2
        assert methods.count("callHierarchy/incomingCalls") == 1
        assert methods.count("callHierarchy/outgoingCalls") == 1
        assert "textDocument/references" not in methods
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_every_prepared_call_item_is_queried_and_results_are_deduplicated(
    repository: Path,
    state_root: Path,
) -> None:
    item = {
        "name": "execute",
        "kind": 12,
        "uri": "$SERVICE_URI",
        "range": {
            "start": {"line": 8, "character": 4},
            "end": {"line": 9, "character": 37},
        },
        "selectionRange": {
            "start": {"line": 8, "character": 8},
            "end": {"line": 8, "character": 15},
        },
    }
    fixture = create_semantic_pyright_fixture(
        repository,
        config={
            "responses": {
                "textDocument/prepareCallHierarchy": [item for _ in range(257)],
                "callHierarchy/incomingCalls": [
                    {
                        "from": {
                            **item,
                            "name": "format_value",
                            "selectionRange": {
                                "start": {"line": 12, "character": 4},
                                "end": {"line": 12, "character": 16},
                            },
                        },
                        "fromRanges": [],
                    }
                ],
            }
        },
    )
    session = _session(repository, state_root, fixture)
    try:
        assert session._sanitize_call_item(
            {
                **item,
                "name": "\ud800",
                "uri": (repository / "pkg/service.py").resolve().as_uri(),
            }
        ) is None
        calls = session.incoming_calls(
            _anchor(repository, "pkg/service.py", 10, 20),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert len(calls.locations) == 1
        methods = [
            event["method"]
            for event in fixture.events()
            if event["kind"] == "client-message"
        ]
        assert methods.count("callHierarchy/incomingCalls") == 257
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_unsupported_calls_never_fall_back_to_references(
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={
            "capabilities": {
                "documentSymbolProvider": True,
                "referencesProvider": True,
            }
        },
    )
    session = _session(repository, state_root, fixture)
    try:
        calls = session.outgoing_calls(
            _anchor(repository, "pkg/service.py", 10, 20),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert calls == ProviderCalls("outgoing", (), "unsupported", True)
        methods = [
            event["method"]
            for event in fixture.events()
            if event["kind"] == "client-message"
        ]
        assert "textDocument/prepareCallHierarchy" not in methods
        assert "textDocument/references" not in methods
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)
