"""Pushed diagnostics and progress of the pinned Pyright session: currency, filtering and bounds."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from llm_wiki import (
    pyright_session_diagnostics,
)
from llm_wiki.pyright_session_core import PyrightSession
from llm_wiki.pyright_session_values import (
    LspDiagnostic,
    LspLocation,
    ProviderDiagnostics,
)
from lsp_positions import LspPosition, LspRange
from repository_scope import resolve_repository_scope

from tests.code_kernel_helpers import (
    SemanticPyrightFixture,
    create_semantic_pyright_fixture,
)
from tests.pyright_session_helpers import (
    _missing_identity,
    _session,
    semantic_pyright,  # noqa: F401
)
from tests.slow_machine import SHORT_TIMEOUT


def _benign_response_results(fixture) -> list[object]:
    return [
        event["response"]["result"]
        for event in fixture.events()
        if event["kind"] == "client-response"
        and event["request_id"] != "semantic-configuration"
    ]


def _runtime_file_names(state_root: Path) -> list[str]:
    return [
        path.relative_to(state_root).as_posix()
        for path in state_root.rglob("*")
        if path.is_file()
    ]


def _runtime_files_matching(state_root: Path, markers) -> list[str]:
    """Runtime files whose path names any of these markers."""
    return [
        name
        for name in _runtime_file_names(state_root)
        if any(marker in name for marker in markers)
    ]


def test_push_diagnostics_are_current_normalized_and_related(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    service_uri = (repository / "pkg/service.py").resolve().as_uri()
    api_uri = (repository / "pkg/api.py").resolve().as_uri()
    try:
        result = session.diagnostics(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        )
        assert result == ProviderDiagnostics(
            (
                LspDiagnostic(
                    service_uri,
                    LspRange(LspPosition(9, 15), LspPosition(9, 25)),
                    2,
                    "reportUnknownMemberType",
                    "Member type is unknown",
                    (
                        (
                            LspLocation(
                                api_uri,
                                LspRange(
                                    LspPosition(1, 8),
                                    LspPosition(1, 14),
                                ),
                            ),
                            "Declared here",
                        ),
                    ),
                ),
            ),
            1,
            False,
        )
        methods = [
            event["method"]
            for event in semantic_pyright.events()
            if event["kind"] == "client-message"
        ]
        assert "textDocument/diagnostic" not in methods
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_stale_lower_diagnostic_version_cannot_replace_current_snapshot(
    repository: Path,
    state_root: Path,
) -> None:
    diagnostic = {
        "range": {
            "start": {"line": 0, "character": 0},
            "end": {"line": 0, "character": 5},
        },
        "severity": 1,
        "code": 1001,
        "message": "current",
    }
    fixture = create_semantic_pyright_fixture(
        repository,
        config={
            "diagnostic_notifications": [
                {
                    "uri": "$REQUEST_URI",
                    "version": 1,
                    "diagnostics": [diagnostic],
                },
                {
                    "uri": "$REQUEST_URI",
                    "version": 0,
                    "diagnostics": [{**diagnostic, "message": "stale"}],
                },
            ]
        },
    )
    session = _session(repository, state_root, fixture)
    try:
        result = session.diagnostics(
            "pkg/service.py",
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert result.document_version == 1
        assert [item.message for item in result.diagnostics] == ["current"]
        assert result.diagnostics[0].code == "1001"
        assert result.partial is False
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_diagnostic_filtering_marks_snapshot_partial_and_drops_external_related(
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={
            "diagnostic_notifications": [
                {
                    "uri": "$REQUEST_URI",
                    "version": 1,
                    "diagnostics": [
                        {
                            "range": {
                                "start": {"line": 0, "character": 0},
                                "end": {"line": 0, "character": 5},
                            },
                            "severity": 2,
                            "message": "kept",
                            "relatedInformation": [
                                {
                                    "location": {
                                        "uri": "$EXTERNAL_URI",
                                        "range": {
                                            "start": {"line": 0, "character": 0},
                                            "end": {"line": 0, "character": 1},
                                        },
                                    },
                                    "message": "external",
                                }
                            ],
                        },
                        {
                            "range": {
                                "start": {"line": -1, "character": 0},
                                "end": {"line": 0, "character": 0},
                            },
                            "severity": 2,
                            "message": "malformed",
                        },
                    ],
                }
            ]
        },
    )
    session = _session(repository, state_root, fixture)
    try:
        result = session.diagnostics(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        )
        assert len(result.diagnostics) == 1
        assert result.diagnostics[0].message == "kept"
        assert result.diagnostics[0].related == ()
        assert result.partial is True
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_diagnostics_wait_for_current_notification_and_timeout_partial(
    repository: Path,
    state_root: Path,
) -> None:
    delayed = create_semantic_pyright_fixture(
        repository,
        config={"diagnostics_delay_seconds": 0.1},
    )
    session = _session(repository, state_root, delayed)
    started = time.monotonic()
    try:
        result = session.diagnostics(
            "pkg/service.py",
            deadline=time.monotonic() + 5,
        )
        assert time.monotonic() - started >= 0.05
        assert result.document_version == 1
        assert result.partial is False
    finally:
        session.close(deadline=time.monotonic() + 5)

    silent = create_semantic_pyright_fixture(
        repository,
        config={"push_diagnostics": False},
    )
    silent_session = _session(repository, state_root, silent)
    try:
        silent_session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        )
        result = silent_session.diagnostics(
            "pkg/service.py",
            deadline=time.monotonic() + 0.25,
        )
        assert result == ProviderDiagnostics((), None, True)
    finally:
        silent_session.close(deadline=time.monotonic() + 5)


def test_not_ready_diagnostics_are_partial_even_when_push_arrived(
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"document_symbol_failures": 10},
    )
    session = _session(repository, state_root, fixture)
    try:
        assert session.diagnostics(
            "pkg/service.py",
            deadline=time.monotonic() + SHORT_TIMEOUT,
        ) == ProviderDiagnostics((), None, True)
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_diagnostics_ignore_unopened_uri(
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"push_diagnostics": False},
    )
    session = _session(repository, state_root, fixture)
    api_uri = (repository / "pkg/api.py").resolve().as_uri()
    diagnostic = {
        "range": {
            "start": {"line": 0, "character": 0},
            "end": {"line": 0, "character": 1},
        },
        "message": "must not be retained",
    }
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + SHORT_TIMEOUT)
        session._publish_diagnostics(
            {"uri": api_uri, "version": 1, "diagnostics": [diagnostic]}
        )

        assert api_uri not in session._diagnostics
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_diagnostic_aggregate_rejection_preserves_previous_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"push_diagnostics": False},
    )
    session = _session(repository, state_root, fixture)
    uri = (repository / "pkg/service.py").resolve().as_uri()
    base = {
        "range": {
            "start": {"line": 0, "character": 0},
            "end": {"line": 0, "character": 1},
        },
    }
    monkeypatch.setattr(
        pyright_session_diagnostics,
        "MAX_DIAGNOSTIC_BYTES",
        512,
        raising=False,
    )
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + SHORT_TIMEOUT)
        session._publish_diagnostics(
            {
                "uri": uri,
                "version": 1,
                "diagnostics": [{**base, "message": "kept"}],
            }
        )
        previous = session._diagnostics[uri]
        session._publish_diagnostics(
            {
                "uri": uri,
                "version": 1,
                "diagnostics": [{**base, "message": "x" * 1024}],
            }
        )

        kept = session._diagnostics[uri]
        assert (kept.diagnostics, kept.partial) == (previous.diagnostics, True)
        assert session._diagnostic_bytes == previous.retained_bytes
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_diagnostic_replacement_and_stale_accounting_are_exact(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"push_diagnostics": False},
    )
    session = _session(repository, state_root, fixture)
    service_uri = (repository / "pkg/service.py").resolve().as_uri()
    api_uri = (repository / "pkg/api.py").resolve().as_uri()
    base = {
        "range": {
            "start": {"line": 0, "character": 0},
            "end": {"line": 0, "character": 1},
        },
    }
    # The cap has to leave room for the URIs themselves: a macOS temporary path
    # runs about ninety characters longer than a Linux one, and a fixed budget
    # evicts the second file there before the policy under test is exercised.
    cap = 1400 + len(service_uri) + len(api_uri)
    monkeypatch.setattr(
        pyright_session_diagnostics,
        "MAX_DIAGNOSTIC_BYTES",
        cap,
        raising=False,
    )
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + SHORT_TIMEOUT)
        session.open_document("pkg/api.py", deadline=time.monotonic() + SHORT_TIMEOUT)
        session._publish_diagnostics(
            {
                "uri": service_uri,
                "version": 1,
                "diagnostics": [{**base, "message": "a" * 600}],
            }
        )
        first_size = session._diagnostic_bytes
        session._publish_diagnostics(
            {
                "uri": service_uri,
                "version": 1,
                "diagnostics": [{**base, "message": "small"}],
            }
        )
        replacement_size = session._diagnostic_bytes
        assert replacement_size < first_size

        session._publish_diagnostics(
            {
                "uri": api_uri,
                "version": 1,
                "diagnostics": [{**base, "message": "b" * 600}],
            }
        )
        assert set(session._diagnostics) == {service_uri, api_uri}
        assert session._diagnostic_bytes == sum(
            snapshot.retained_bytes for snapshot in session._diagnostics.values()
        )
        assert session._diagnostic_bytes <= cap

        retained_bytes = session._diagnostic_bytes
        retained = session._diagnostics[service_uri]
        session._publish_diagnostics(
            {
                "uri": service_uri,
                "version": 0,
                "diagnostics": [{**base, "message": "stale" * 200}],
            }
        )
        assert session._diagnostics[service_uri] is retained
        assert session._diagnostic_bytes == retained_bytes
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_benign_server_requests_and_progress_are_bounded_in_memory_only(
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"benign_requests": True, "push_progress": True},
    )
    session = _session(repository, state_root, fixture)
    try:
        session.start(deadline=time.monotonic() + SHORT_TIMEOUT)
        assert session.progress_events == (
            ("$/progress", "semantic-progress", "begin", "Analyzing"),
            ("pyright/beginProgress",),
            ("pyright/reportProgress", "Analyzing files"),
            ("pyright/endProgress",),
        )
        assert _benign_response_results(fixture) == [None, None, None]
        assert session.capabilities["implementations"] is False
        assert not _runtime_files_matching(state_root, ("progress", "diagnostic"))
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_progress_retention_has_count_and_aggregate_byte_bounds(
    repository: Path,
    state_root: Path,
) -> None:
    session = PyrightSession(
        resolve_repository_scope(repository),
        _missing_identity(),
        state_root=state_root,
    )
    for index in range(300):
        session._progress(
            "$/progress",
            {
                "token": f"{index:03d}" + "t" * 253,
                "value": {"kind": "report", "message": "m" * 4096},
            },
        )

    events = session.progress_events
    assert len(events) <= 256
    assert sum(
        len(value.encode("utf-8"))
        for event in events
        for value in event
        if isinstance(value, str)
    ) <= 1024 * 1024
