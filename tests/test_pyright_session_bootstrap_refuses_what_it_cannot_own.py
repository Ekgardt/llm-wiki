from __future__ import annotations

import dataclasses
import time
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest
from code_intelligence import PositionEncoding
from llm_wiki.pyright_session_bootstrap import (
    _check_qualified_identity,
    _parse_server_capabilities,
)
from llm_wiki.pyright_session_core import LanguageServerSession
from llm_wiki.pyright_session_values import BootstrapDegradation, OpenDocument
from lsp_profiles import GOPLS_PROFILE, PYRIGHT_PROFILE, TYPESCRIPT_PROFILE
from lsp_protocol import LspProtocol
from lsp_server_profile import LanguageServerProfile
from pyright_profile import PYRIGHT_INITIALIZATION_OPTIONS_SHA256, PyrightIdentity
from repository_scope import resolve_repository_scope

from tests.language_server_session_state import document_of, unstarted_session
from tests.pyright_session_helpers import _missing_identity

_OWNER = "owner-accepted"
_GENERATION = "generation-new"


class _ScriptedProtocol:
    def __init__(
        self, answers: Mapping[str, object], refused_notification: str | None = None
    ) -> None:
        self.answers = dict(answers)
        self.refused_notification = refused_notification
        self.notified: list[str] = []

    def request(self, method: str, params: object, *, deadline: float) -> object:
        return self.answers.get(method)

    def notify(self, method: str, params: object, *, deadline: float) -> None:
        self.notified.append(method)
        if method == self.refused_notification:
            raise RuntimeError(f"{method} refused")


@pytest.fixture
def session(repository: Path, state_root: Path) -> LanguageServerSession:
    owned = unstarted_session(repository, state_root)
    owned._bootstrap_owner_nonce = _OWNER
    owned._bootstrap_generation_owners[_GENERATION] = _OWNER
    return owned


@pytest.fixture
def document(session: LanguageServerSession) -> OpenDocument:
    opened = document_of(session, "pkg/service.py")
    session._documents[opened.source.uri] = opened
    return opened


def _deadline() -> float:
    return time.monotonic() + 5.0


def _qualified(
    *,
    status: str = "qualified",
    degradation_codes: tuple[str, ...] = (),
    initialization_options_sha256: str = PYRIGHT_INITIALIZATION_OPTIONS_SHA256,
    configuration_sha256: str = "c" * 64,
    executable_sha256: str | None = "d" * 64,
) -> PyrightIdentity:
    return dataclasses.replace(
        _missing_identity(),
        status=status,
        qualified=True,
        degradation_codes=degradation_codes,
        initialization_options_sha256=initialization_options_sha256,
        configuration_sha256=configuration_sha256,
        executable_sha256=executable_sha256,
    )


def _profiled(
    repository: Path, state_root: Path, profile: LanguageServerProfile
) -> LanguageServerSession:
    return LanguageServerSession(
        resolve_repository_scope(repository),
        _missing_identity(),
        state_root=state_root,
        profile=profile,
    )


def _protocol(protocol: _ScriptedProtocol) -> LspProtocol:
    return cast(LspProtocol, protocol)


@pytest.mark.parametrize("result", ["x", {"capabilities": "x"}])
def test_an_initialize_result_without_capabilities_is_a_named_degradation(
    result: object,
) -> None:
    with pytest.raises(BootstrapDegradation) as raised:
        _parse_server_capabilities(result)

    assert raised.value.code == "pyright_initialize_result_invalid"


def test_a_consistent_qualified_identity_is_accepted() -> None:
    _check_qualified_identity(_qualified())


@pytest.mark.parametrize(
    ("identity", "message"),
    [
        (_qualified(status="missing"), "internally inconsistent"),
        (_qualified(degradation_codes=("x",)), "internally inconsistent"),
        (_qualified(initialization_options_sha256="0" * 64), "initialization options"),
        (_qualified(configuration_sha256="x"), "configuration identity is invalid"),
        (_qualified(executable_sha256=None), "executable identity is invalid"),
    ],
)
def test_an_inconsistent_qualified_identity_is_refused(
    identity: PyrightIdentity, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _check_qualified_identity(identity)


def test_another_profile_checks_the_options_digest_it_derives_itself() -> None:
    with pytest.raises(ValueError, match="initialization options identity"):
        _check_qualified_identity(_qualified(), TYPESCRIPT_PROFILE)


def test_a_bootstrap_for_an_owner_no_longer_accepted_is_refused(
    session: LanguageServerSession,
) -> None:
    with pytest.raises(RuntimeError, match="owner is no longer accepted"):
        session._bootstrap_owned_generation(
            "owner-retired", _protocol(_ScriptedProtocol({})), 1, "other", _deadline()
        )

    assert "other" not in session._bootstrap_generation_owners


def test_a_generation_without_an_owner_is_refused(session: LanguageServerSession) -> None:
    with pytest.raises(RuntimeError, match="owner is no longer accepted"):
        session._bootstrap_owner("generation-unowned")


def test_a_replaced_owner_is_refused(session: LanguageServerSession) -> None:
    with pytest.raises(RuntimeError, match="owner is no longer accepted"):
        session._require_bootstrap_owner("owner-retired")


def test_an_owner_replaced_while_the_wire_generation_began_is_refused(
    session: LanguageServerSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    def replace_owner(generation_nonce: str) -> None:
        session._bootstrap_owner_nonce = "owner-replaced"

    monkeypatch.setattr(session, "_begin_wire_generation", replace_owner)

    with pytest.raises(RuntimeError, match="owner is no longer accepted"):
        session._adopt_generation(_OWNER, _GENERATION, {}, PositionEncoding.UTF16)

    assert session._generation_nonce is None


def test_reopening_a_document_on_a_retired_generation_is_refused(
    session: LanguageServerSession, document: OpenDocument
) -> None:
    session._begin_wire_generation(_GENERATION)
    protocol = _ScriptedProtocol({})

    with pytest.raises(RuntimeError, match="generation changed during didOpen"):
        session._reopen_documents(
            (document,), _protocol(protocol), _OWNER, "generation-retired", _deadline()
        )

    assert protocol.notified == []


def test_a_document_whose_symbols_come_back_partial_is_not_marked_ready(
    session: LanguageServerSession, document: OpenDocument
) -> None:
    session._generation_nonce = _GENERATION
    protocol = _ScriptedProtocol({"textDocument/documentSymbol": "x"})

    session._prime_one_document(document, _protocol(protocol), _GENERATION, _deadline())

    assert session._ready_uri_generations == {}


def test_a_failed_reopen_leaves_the_session_degraded_and_forgets_the_generation(
    session: LanguageServerSession, document: OpenDocument
) -> None:
    session._ready_uri_generations[document.source.uri] = "generation-old"
    protocol = _ScriptedProtocol(
        {"initialize": {"capabilities": {"documentSymbolProvider": True}}},
        refused_notification="textDocument/didOpen",
    )

    with pytest.raises(RuntimeError, match="didOpen refused"):
        session._bootstrap_generation(_protocol(protocol), 1, _GENERATION, _deadline())

    assert (session._readiness, session._readiness_evidence) == ("not_ready", ())
    assert session._ready_uri_generations == {}
    assert session._wire_generation is None
    assert PYRIGHT_PROFILE.degradation_code("restart_bootstrap_failed") in (
        session.degradation_codes
    )


def test_a_native_server_is_its_own_interpreter(
    repository: Path, state_root: Path, tmp_path: Path
) -> None:
    native = _profiled(repository, state_root, GOPLS_PROFILE)
    server = tmp_path / "gopls"

    assert native._validated_interpreter(server, _deadline()) == server


def test_a_progress_gated_profile_names_the_progress_end_as_evidence(
    repository: Path, state_root: Path
) -> None:
    gated = _profiled(repository, state_root, TYPESCRIPT_PROFILE)

    assert gated._query_ready_evidence()[-1] == "workDoneProgress/end"
