from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast

from code_intelligence import PositionEncoding
from llm_wiki.pyright_session_core import LanguageServerSession
from llm_wiki.pyright_session_values import OpenDocument
from lsp_process import LspProcess
from lsp_security import resolve_repository_source
from repository_scope import resolve_repository_scope

from tests.pyright_session_helpers import _missing_identity

READY_GENERATION = "generation-ready"


class ScriptedProcess:
    def __init__(
        self,
        answers: Mapping[str, object] | None = None,
        on_request: Callable[[str], None] | None = None,
    ) -> None:
        self.answers = dict(answers or {})
        self.on_request = on_request
        self.requests: list[tuple[str, object]] = []

    def request(self, method: str, params: object, *, deadline: float) -> object:
        self.requests.append((method, params))
        if self.on_request is not None:
            self.on_request(method)
        return self.answers.get(method)

    def requested_methods(self) -> list[str]:
        return [method for method, _params in self.requests]


def unstarted_session(repository: Path, state_root: Path) -> LanguageServerSession:
    return LanguageServerSession(
        resolve_repository_scope(repository), _missing_identity(), state_root=state_root
    )


def document_of(session: LanguageServerSession, path: str, version: int = 1) -> OpenDocument:
    source = resolve_repository_source(session._repository, path)
    content = source.absolute_path.read_bytes()
    return OpenDocument(source, content, hashlib.sha256(content).hexdigest(), version)


def make_query_ready(
    session: LanguageServerSession, document: OpenDocument, process: ScriptedProcess
) -> LspProcess:
    live = cast(LspProcess, process)
    uri = document.source.uri
    session._process = live
    session._position_encoding = PositionEncoding.UTF16
    session._generation_nonce = READY_GENERATION
    session._documents[uri] = document
    session._ready_uri_generations[uri] = READY_GENERATION
    session._begin_wire_generation(READY_GENERATION)
    session._wire_opened.add((READY_GENERATION, uri, document.version))
    return live


def retire_generation(session: LanguageServerSession) -> None:
    session._generation_nonce = "generation-replaced"
