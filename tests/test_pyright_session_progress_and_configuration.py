from __future__ import annotations

from pathlib import Path

import pytest
from llm_wiki.pyright_session_core import LanguageServerSession
from llm_wiki.pyright_session_progress import configuration_result
from lsp_profiles import PYRIGHT_PROFILE
from repository_scope import resolve_repository_scope

from tests.pyright_session_helpers import _missing_identity


def _pyright_settings() -> dict[str, object]:
    settings = PYRIGHT_PROFILE.wire_configuration()
    assert isinstance(settings, dict)
    return settings


@pytest.fixture
def session(repository: Path, state_root: Path) -> LanguageServerSession:
    return LanguageServerSession(
        resolve_repository_scope(repository), _missing_identity(), state_root=state_root
    )


@pytest.mark.parametrize(
    "params",
    [
        "not an object",
        {"token": "t", "value": {"kind": "begin"}, "extra": 1},
        {"token": "t", "value": "begin"},
        {"token": True, "value": {"kind": "begin"}},
        {"token": 1.5, "value": {"kind": "begin"}},
        {"token": "t", "value": {"kind": "unknown"}},
        {"token": "t", "value": {"kind": "begin", "title": 7}},
        {"token": "t", "value": {"kind": "report", "message": "x" * 5000}},
    ],
)
def test_a_malformed_progress_notification_is_not_retained(
    session: LanguageServerSession, params: object
) -> None:
    session._progress("$/progress", params)

    assert session.progress_events == ()


def test_a_well_formed_progress_notification_is_retained_with_its_text(
    session: LanguageServerSession,
) -> None:
    session._progress("$/progress", {"token": 4, "value": {"kind": "begin", "title": "Load"}})
    session._progress("$/progress", {"token": "t", "value": {"kind": "end"}})

    assert session.progress_events == (
        ("$/progress", 4, "begin", "Load"),
        ("$/progress", "t", "end", ""),
    )


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("pyright/beginProgress", {"unexpected": 1}),
        ("pyright/reportProgress", {"message": 42}),
        ("pyright/reportProgress", 42),
        ("vendor/unknownProgress", None),
    ],
)
def test_vendor_progress_without_a_usable_payload_is_not_retained(
    session: LanguageServerSession, method: str, params: object
) -> None:
    session._progress(method, params)

    assert session.progress_events == ()


def test_vendor_progress_markers_and_reports_are_retained(
    session: LanguageServerSession,
) -> None:
    session._progress("pyright/beginProgress", None)
    session._progress("pyright/reportProgress", {"message": "Analyzing"})
    session._progress("pyright/endProgress", {})

    assert session.progress_events == (
        ("pyright/beginProgress",),
        ("pyright/reportProgress", "Analyzing"),
        ("pyright/endProgress",),
    )


def test_an_event_larger_than_the_whole_progress_ring_is_dropped(
    session: LanguageServerSession,
) -> None:
    session._retain_progress(("x" * (1024 * 1024),))

    assert session.progress_events == ()


@pytest.mark.parametrize(
    ("token", "retained"),
    [(True, set()), (2.5, set()), (9, {9}), ("load", {"load"})],
)
def test_work_done_create_remembers_only_usable_tokens(
    session: LanguageServerSession, token: object, retained: set[object]
) -> None:
    session._work_done_progress_create({"token": token})

    assert session._work_done_tokens == retained


def test_an_end_on_a_malformed_payload_does_not_declare_the_load_finished(
    session: LanguageServerSession,
) -> None:
    session._work_done_tokens.add("load")
    session._generation_nonce = "generation"

    session._progress("$/progress", ["load", "end"])

    assert session._progress_ready_generation is None


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ("text", "server request params must be an object"),
        ({str(index): index for index in range(17)}, "server request params exceed their bound"),
    ],
)
def test_a_server_request_with_unusable_params_is_refused(
    session: LanguageServerSession, params: object, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        session._benign_server_request(params)


def test_a_configuration_request_naming_unknown_fields_gets_no_items(
    session: LanguageServerSession,
) -> None:
    assert session._configuration({"items": [{"section": "python"}], "other": 1}) == []


@pytest.mark.parametrize(
    "item",
    [
        "python",
        {"section": "python", "unexpected": True},
        {"scopeUri": 12, "section": "python"},
        {"section": ""},
        {"section": 3},
        {"section": chr(0xD800)},
        {"section": "s" * 257},
        {"section": "python..analysis"},
        {"section": "python.analysis.logLevel.deeper"},
    ],
)
def test_an_unusable_configuration_item_is_answered_with_null(item: object) -> None:
    assert configuration_result(_pyright_settings(), item) is None


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ({"section": "python.analysis.logLevel"}, "Error"),
        ({"scopeUri": "file:///repository", "section": "pyright.disableOrganizeImports"}, True),
    ],
)
def test_a_usable_configuration_item_is_answered_from_the_profile_settings(
    item: object, expected: object
) -> None:
    assert configuration_result(_pyright_settings(), item) == expected


def test_an_item_without_a_section_is_answered_with_the_whole_configuration() -> None:
    settings = _pyright_settings()

    assert configuration_result(settings, {}) == settings
