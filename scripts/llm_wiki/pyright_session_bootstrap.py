"""Initialize one server generation, adopt it, and keep each document's readiness evidence."""

import os
import re
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

from code_intelligence import PositionEncoding
from llm_wiki.pyright_session_paths import validated_local_file
from llm_wiki.pyright_session_values import BootstrapDegradation, OpenDocument
from lsp_positions import path_to_file_uri
from lsp_process import LspProcess, ProcessState
from lsp_profiles import PYRIGHT_PROFILE
from lsp_protocol import JsonRpcResponseError, LspProtocol
from lsp_server_profile import LanguageServerProfile, thaw_profile_value
from pyright_profile import PYRIGHT_INITIALIZATION_OPTIONS_SHA256, PyrightIdentity

_SHA256 = re.compile(r"[0-9a-f]{64}")


_CAPABILITY_FIELDS = {
    "calls": "callHierarchyProvider",
    "definition": "definitionProvider",
    "document_symbols": "documentSymbolProvider",
    "hover": "hoverProvider",
    "implementations": "implementationProvider",
    "references": "referencesProvider",
    "type_definition": "typeDefinitionProvider",
    "workspace_symbols": "workspaceSymbolProvider",
}


CLIENT_CAPABILITIES = {
    "general": {"positionEncodings": ("utf-8", "utf-16", "utf-32")},
    "textDocument": {
        "callHierarchy": {"dynamicRegistration": False},
        "definition": {"dynamicRegistration": False, "linkSupport": True},
        "documentSymbol": {
            "dynamicRegistration": False,
            "hierarchicalDocumentSymbolSupport": True,
        },
        "hover": {
            "dynamicRegistration": False,
            "contentFormat": ("plaintext",),
        },
        "implementation": {"dynamicRegistration": False, "linkSupport": True},
        "publishDiagnostics": {
            "relatedInformation": True,
            "versionSupport": True,
        },
        "references": {"dynamicRegistration": False},
        "typeDefinition": {"dynamicRegistration": False, "linkSupport": True},
    },
    "window": {"workDoneProgress": True},
    "workspace": {
        "configuration": True,
        "symbol": {"dynamicRegistration": False},
        "workspaceFolders": True,
    },
}


def _provider_supported(value: object, label: str, prefix: str) -> bool:
    if value is None or value is False:
        return False
    if value is True or isinstance(value, dict):
        return True
    raise BootstrapDegradation(f"{prefix}_{label}_capability_invalid")


_POSITION_ENCODINGS = MappingProxyType(
    {
        "utf-8": PositionEncoding.UTF8,
        "utf-16": PositionEncoding.UTF16,
        "utf-32": PositionEncoding.UTF32,
    }
)


def _server_position_encoding(
    server: Mapping[str, object], prefix: str
) -> PositionEncoding:
    """The encoding the server asked for, from the three we can serve."""
    value = server.get("positionEncoding", "utf-16")
    if not isinstance(value, str) or value not in _POSITION_ENCODINGS:
        raise BootstrapDegradation(f"{prefix}_position_encoding_unsupported")
    return _POSITION_ENCODINGS[value]


def _parse_server_capabilities(
    result: object,
    prefix: str = "pyright",
) -> tuple[dict[str, bool], PositionEncoding]:
    if not isinstance(result, dict) or not isinstance(result.get("capabilities"), dict):
        raise BootstrapDegradation(f"{prefix}_initialize_result_invalid")
    server = result["capabilities"]
    encoding = _server_position_encoding(server, prefix)
    capabilities = {
        name: _provider_supported(server.get(field), name, prefix)
        for name, field in _CAPABILITY_FIELDS.items()
    }
    capabilities["diagnostics"] = True
    return dict(sorted(capabilities.items())), encoding


def _check_qualified_identity(
    identity: PyrightIdentity, profile: LanguageServerProfile = PYRIGHT_PROFILE
) -> None:
    """A qualified identity has to be internally consistent before it is used."""
    if identity.status != "qualified" or identity.degradation_codes:
        raise ValueError("qualified language server identity is internally inconsistent")
    if identity.initialization_options_sha256 != _profile_options_digest(profile):
        raise ValueError("language server initialization options identity is inconsistent")
    _check_identity_digest(
        identity.configuration_sha256, "Pyright configuration identity is invalid"
    )
    _check_identity_digest(
        identity.executable_sha256, "Pyright executable identity is invalid"
    )


def _profile_options_digest(profile: LanguageServerProfile) -> str:
    """Pyright keeps its precomputed constant; anything else derives its own."""
    if profile is PYRIGHT_PROFILE:
        return PYRIGHT_INITIALIZATION_OPTIONS_SHA256
    from lsp_identity import profile_initialization_options_sha256

    return profile_initialization_options_sha256(profile)


def _check_identity_digest(value: object, message: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(message)


_PROTOCOL_EVIDENCE = ("initialize", "initialized", "configuration")


_DID_OPEN_EVIDENCE = (*_PROTOCOL_EVIDENCE, "didOpen")


_QUERY_READY_EVIDENCE = (*_DID_OPEN_EVIDENCE, "documentSymbol")


class SessionBootstrap:
    """Bootstrap server generations and track readiness evidence."""

    def _bootstrap_owned_generation(
        self,
        owner_nonce: str,
        protocol: LspProtocol,
        process_id: int,
        generation_nonce: str,
        deadline: float,
    ) -> ProcessState:
        with self._lock:
            if self._bootstrap_owner_nonce != owner_nonce:
                raise RuntimeError("Pyright process owner is no longer accepted")
            self._bootstrap_generation_owners[generation_nonce] = owner_nonce
        try:
            return self._bootstrap_generation(
                protocol,
                process_id,
                generation_nonce,
                deadline,
            )
        finally:
            self._release_generation_owner(generation_nonce, owner_nonce)

    def _release_generation_owner(self, generation_nonce: str, owner_nonce: str) -> None:
        with self._lock:
            if self._bootstrap_generation_owners.get(generation_nonce) == owner_nonce:
                self._bootstrap_generation_owners.pop(generation_nonce, None)

    def _bootstrap_owner(self, generation_nonce: str) -> str:
        """The owner nonce this bootstrap belongs to, while it is still accepted."""
        with self._lock:
            owner_nonce = self._bootstrap_generation_owners.get(generation_nonce)
            if owner_nonce is None or self._bootstrap_owner_nonce != owner_nonce:
                raise RuntimeError("Pyright process owner is no longer accepted")
        return owner_nonce

    def _require_bootstrap_owner(self, owner_nonce: str) -> None:
        """The owner that started this bootstrap must still be the accepted one."""
        with self._lock:
            if self._bootstrap_owner_nonce != owner_nonce:
                raise RuntimeError("Pyright process owner is no longer accepted")

    def _initialize_protocol(
        self, protocol: LspProtocol, deadline: float
    ) -> tuple[dict[str, bool], PositionEncoding]:
        """Initialize the server, then hand it our configuration."""
        root = Path(self._repository.checkout_root)
        root_uri = path_to_file_uri(root)
        result = protocol.request(
            "initialize",
            {
                "processId": os.getpid(),
                "clientInfo": {"name": "llm-wiki"},
                "rootUri": root_uri,
                "workspaceFolders": [{"uri": root_uri, "name": root.name}],
                "initializationOptions": self._profile.wire_initialization_options(
                    self._state_root
                ),
                "capabilities": thaw_profile_value(CLIENT_CAPABILITIES),
            },
            deadline=deadline,
        )
        capabilities, encoding = _parse_server_capabilities(
            result, self._profile.degradation_prefix
        )
        protocol.notify("initialized", {}, deadline=deadline)
        protocol.notify(
            "workspace/didChangeConfiguration",
            {"settings": self._profile.wire_configuration()},
            deadline=deadline,
        )
        return capabilities, encoding

    def _publish_generation_locked(
        self,
        generation_nonce: str,
        capabilities: dict[str, bool],
        encoding: PositionEncoding,
    ) -> tuple[OpenDocument, ...]:
        """Make the new generation current and report the documents to reopen."""
        documents = tuple(self._documents.values())
        self._generation_nonce = generation_nonce
        self._ready_uri_generations.clear()
        self._diagnostics.clear()
        self._diagnostic_bytes = 0
        self._capabilities = capabilities
        self._position_encoding = encoding
        self._readiness_evidence = (
            "initialize",
            "initialized",
            "configuration",
        )
        self._readiness = "protocol_initialized"
        return documents

    def _adopt_generation(
        self,
        owner_nonce: str,
        generation_nonce: str,
        capabilities: dict[str, bool],
        encoding: PositionEncoding,
    ) -> tuple[OpenDocument, ...]:
        self._require_bootstrap_owner(owner_nonce)
        self._begin_wire_generation(generation_nonce)
        with self._lock:
            if self._bootstrap_owner_nonce != owner_nonce:
                raise RuntimeError("Pyright process owner is no longer accepted")
            return self._publish_generation_locked(
                generation_nonce, capabilities, encoding
            )

    def _reopen_documents(
        self,
        documents: tuple[OpenDocument, ...],
        protocol: LspProtocol,
        owner_nonce: str,
        generation_nonce: str,
        deadline: float,
    ) -> None:
        """Every document we hold open is opened again on the new generation."""
        for document in documents:
            self._require_bootstrap_owner(owner_nonce)
            if not self._send_protocol_did_open(
                document,
                protocol,
                generation_nonce,
                deadline=deadline,
            ):
                raise RuntimeError("Pyright generation changed during didOpen")

    def _mark_document_ready(self, uri: str, generation_nonce: str) -> None:
        with self._lock:
            if self._generation_nonce == generation_nonce:
                self._ready_uri_generations[uri] = generation_nonce

    def _prime_one_document(
        self,
        document: OpenDocument,
        protocol: LspProtocol,
        generation_nonce: str,
        deadline: float,
    ) -> None:
        """A document whose symbols come back complete counts as query-ready."""
        try:
            symbols = protocol.request(
                "textDocument/documentSymbol",
                {"textDocument": {"uri": document.source.uri}},
                deadline=deadline,
            )
        except JsonRpcResponseError:
            return
        if self._normalize_document_symbols(symbols, document.source.uri)[1]:
            return
        self._mark_document_ready(document.source.uri, generation_nonce)

    def _prime_document_readiness(
        self,
        documents: tuple[OpenDocument, ...],
        protocol: LspProtocol,
        generation_nonce: str,
        deadline: float,
    ) -> None:
        for document in documents:
            self._prime_one_document(document, protocol, generation_nonce, deadline)

    def _bootstrap_ready_state(self, deadline: float) -> ProcessState:
        with self._lock:
            self._refresh_readiness_locked(deadline=deadline)
            ready = self._readiness == "query_ready"
        if ready:
            return ProcessState.WORKSPACE_READY
        return ProcessState.PROTOCOL_INITIALIZED

    def _fail_bootstrap_locked(self) -> None:
        """A failed re-bootstrap leaves the session degraded, not quietly ready."""
        self._readiness = "not_ready"
        self._readiness_evidence = ()
        self._ready_uri_generations.clear()
        self._degradation_codes = tuple(
            sorted(
                {
                    *self._degradation_codes,
                    self._profile.degradation_code("restart_bootstrap_failed"),
                }
            )
        )

    def _fail_generation_bootstrap(
        self, documents: tuple[OpenDocument, ...], generation_nonce: str
    ) -> None:
        if documents:
            with self._lock:
                self._fail_bootstrap_locked()
        self._discard_wire_generation(generation_nonce)

    def _bootstrap_generation(
        self,
        protocol: LspProtocol,
        _process_id: int,
        _generation_nonce: str,
        deadline: float,
    ) -> ProcessState:
        owner_nonce = self._bootstrap_owner(_generation_nonce)
        self._rearm_progress_gate()
        capabilities, encoding = self._initialize_protocol(protocol, deadline)
        documents: tuple[OpenDocument, ...] = ()
        try:
            documents = self._adopt_generation(
                owner_nonce, _generation_nonce, capabilities, encoding
            )
            self._reopen_documents(
                documents, protocol, owner_nonce, _generation_nonce, deadline
            )
            if capabilities["document_symbols"]:
                self._prime_document_readiness(
                    documents, protocol, _generation_nonce, deadline
                )
                return self._bootstrap_ready_state(deadline)
        except BaseException:
            self._fail_generation_bootstrap(documents, _generation_nonce)
            raise
        return ProcessState.PROTOCOL_INITIALIZED

    def _validated_qualified_paths(self, *, deadline: float) -> tuple[Path, Path]:
        """The installed paths, or a named degradation when the install is not usable.

        A missing or inconsistent install raised `ValueError`, which re-armed the
        start with no backoff and recorded nothing, so every query launched it
        again (audit B-39,
        docs/research/2026-09-25-a-broken-install-is-a-named-degradation.md).
        """
        try:
            return self._checked_install_paths(deadline)
        except (TypeError, ValueError) as error:
            raise BootstrapDegradation(
                f"{self._profile.degradation_prefix}_install_invalid"
            ) from error

    def _checked_install_paths(self, deadline: float) -> tuple[Path, Path]:
        identity = self._identity
        _check_qualified_identity(identity, self._profile)
        server = validated_local_file(
            identity.server_executable,
            "server_executable",
            deadline=deadline,
        )
        return (self._validated_interpreter(server, deadline), server)

    def _validated_interpreter(self, server: Path, deadline: float) -> Path:
        """The Node that runs the server, or the server itself when it is native.

        gopls is an executable; there is no interpreter to validate and none to
        launch it with. Research:
        `docs/research/2026-09-12-installing-go-and-building-gopls.md`.
        """
        if self._profile.native:
            return server
        return validated_local_file(
            self._identity.node_executable,
            "node_executable",
            deadline=deadline,
        )

    def _reset_readiness_locked(self) -> None:
        """Forget what a running server told us about itself."""
        self._readiness = "not_ready"
        self._readiness_evidence = ()
        self._position_encoding = None
        self._capabilities = {}

    def _degrade_locked(self, code: str) -> None:
        """Record a startup degradation and drop what it invalidates."""
        self._reset_readiness_locked()
        self._degradation_codes = tuple(sorted({*self._degradation_codes, code}))

    def _forget_generation_locked(self) -> None:
        """Forget everything tied to a generation that never became ours."""
        self._work_done_tokens.clear()
        self._progress_ready_generation = None
        self._generation_nonce = None
        self._ready_uri_generations.clear()
        self._workspace_revision = None
        self._diagnostics.clear()
        self._diagnostic_bytes = 0
        self._clear_wire_state()

    def _document_ready_locked(self, uri: str) -> bool:
        # The progress gate belongs here rather than in
        # `_refresh_readiness_locked`: readiness reporting goes through that
        # one, but a query is admitted through `_document_query` ->
        # `_query_ready_locked` -> `_document_query_current_locked` -> here.
        # Gating only the reporting path would leave a session that calls
        # itself not ready and answers anyway.
        generation = self._generation_nonce
        document = self._documents.get(uri)
        return (
            generation is not None
            and document is not None
            and self._progress_gate_satisfied_locked()
            and self._ready_uri_generations.get(uri) == generation
            and self._wire_document_opened(document, generation)
        )

    def _semantic_query_epoch_locked(self) -> int | None:
        epoch = self._synchronize_epoch
        return epoch if epoch % 2 == 0 else None

    def _semantic_query_epoch_current_locked(self, epoch: int) -> bool:
        return epoch % 2 == 0 and self._synchronize_epoch == epoch

    def _document_query_current_locked(
        self,
        process: LspProcess,
        generation: str,
        document: OpenDocument,
        synchronize_epoch: int,
    ) -> bool:
        uri = document.source.uri
        return (
            self._semantic_query_epoch_current_locked(synchronize_epoch)
            and self._process is process
            and self._generation_nonce == generation
            and self._documents.get(uri) is document
            and self._document_ready_locked(uri)
        )

    def _demote_target_locked(self, target: str) -> None:
        """The target is open but could not be promoted to query-ready."""
        self._ready_uri_generations.pop(target, None)
        self._readiness = "protocol_initialized"
        self._readiness_evidence = _DID_OPEN_EVIDENCE

    def _promote_target_locked(
        self,
        target: str,
        process: LspProcess,
        generation: str,
        deadline: float,
    ) -> bool:
        """Whether the server confirmed its workspace is ready."""
        try:
            return process.promote_workspace_ready(
                generation_nonce=generation,
                deadline=deadline,
            )
        except BaseException:
            self._demote_target_locked(target)
            raise

    def _target_ready_locked(self, target: str, deadline: float) -> bool:
        """Whether the ready target may be reported as query-ready."""
        process = self._process
        generation = self._generation_nonce
        if process is None or generation is None:
            return True
        if process.generation_nonce != generation:
            return True
        return self._promoted_or_demoted_locked(target, process, generation, deadline)

    def _promoted_or_demoted_locked(
        self, target: str, process: LspProcess, generation: str, deadline: float
    ) -> bool:
        if self._promote_target_locked(target, process, generation, deadline):
            return True
        self._demote_target_locked(target)
        return False

    def _initialized_readiness_locked(self) -> None:
        """Without a ready target the session is initialized, not query-ready."""
        generation = self._generation_nonce
        self._readiness = "protocol_initialized"
        did_open = any(
            self._wire_document_opened(document, generation)
            for document in self._documents.values()
        )
        self._readiness_evidence = (
            *_PROTOCOL_EVIDENCE,
            *(("didOpen",) if did_open else ()),
        )

    def _refresh_readiness_locked(self, *, deadline: float) -> None:
        if self._position_encoding is None:
            self._readiness = "not_ready"
            self._readiness_evidence = ()
            return
        target = self._readiness_target_uri
        if target is None or not self._document_ready_locked(target):
            self._initialized_readiness_locked()
            return
        self._promote_readiness_locked(target, deadline)

    def _promote_readiness_locked(self, target: str, deadline: float) -> None:
        if not self._target_ready_locked(target, deadline):
            return
        self._readiness = "query_ready"
        self._readiness_evidence = self._query_ready_evidence()

    def _query_ready_evidence(self) -> tuple[str, ...]:
        if not self._profile.gates_on_progress():
            return _QUERY_READY_EVIDENCE
        return (*_QUERY_READY_EVIDENCE, "workDoneProgress/end")

    def _mark_protocol_initialized(self, *, did_open: bool, deadline: float) -> None:
        with self._lock:
            if not did_open and self._readiness_target_uri is not None:
                self._ready_uri_generations.pop(self._readiness_target_uri, None)
            self._refresh_readiness_locked(deadline=deadline)
