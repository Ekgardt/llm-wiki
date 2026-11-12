"""Restart, readiness fences and close of the pinned Pyright session."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import get_type_hints

import lsp_process
import pytest
from llm_wiki.pyright_session_core import PyrightSession
from llm_wiki.pyright_session_values import (
    OpenDocument,
    ProviderCalls,
    ProviderDiagnostics,
    ProviderHover,
    ProviderLocations,
)
from lsp_positions import SourceAnchor
from lsp_process import LspProcess, ProcessState
from lsp_protocol import ProtocolViolation
from repository_scope import resolve_repository_scope
from workspace_revision import (
    WorkspaceDelta,
    compute_workspace_revision,
)

from tests.code_kernel_helpers import (
    SemanticPyrightFixture,
    create_semantic_pyright_fixture,
)
from tests.pyright_session_helpers import (
    _anchor,
    _client_events,
    _client_pids,
    _copied_server_identity,
    _missing_identity,
    _session,
    semantic_pyright,  # noqa: F401
)
from tests.slow_machine import SHORT_TIMEOUT


def _hold_first_response(count: int, ready, release, deadline: float) -> None:
    """Only the first semantic response waits for the test to release it."""
    if count != 1:
        return
    ready.set()
    if not release.wait(max(0.0, deadline - time.monotonic())):
        raise TimeoutError("semantic response release expired")


def _hold_at_barrier(ready, release, deadline: float) -> None:
    """Publish that this stage completed, then wait for the test to release it."""
    ready.set()
    if not release.wait(max(0.0, deadline - time.monotonic())):
        raise TimeoutError("call prepare publication barrier expired")


def _definition_requests(fixture) -> int:
    return sum(
        event.get("method") == "textDocument/definition" for event in fixture.events()
    )


def _generation_methods(messages, pid: int) -> list[str]:
    return [event["method"] for event in messages if event["pid"] == pid]


def _opened_document(messages, pid: int) -> dict:
    return next(
        event["message"]["params"]["textDocument"]
        for event in messages
        if event["pid"] == pid and event["method"] == "textDocument/didOpen"
    )


def _assert_generation_replayed(messages, pid: int, content: str) -> None:
    """Each server generation gets the same opening sequence and document."""
    assert _generation_methods(messages, pid)[:5] == [
        "initialize",
        "initialized",
        "workspace/didChangeConfiguration",
        "textDocument/didOpen",
        "textDocument/documentSymbol",
    ]
    opened = _opened_document(messages, pid)
    assert opened["version"] == 1
    assert opened["text"] == content


def test_transparent_restart_requires_fresh_query_after_reopen_and_reprobe(
    repository: Path,
    state_root: Path,
) -> None:
    marker = repository / ".fake-lsp-crashed"
    fixture = create_semantic_pyright_fixture(
        repository,
        config={
            "crash_once_method": "textDocument/definition",
            "crash_marker": str(marker),
        },
    )
    session = _session(repository, state_root, fixture)
    content = (repository / "pkg/service.py").read_text(encoding="utf-8")
    try:
        initial_diagnostics = session.diagnostics(
            "pkg/service.py",
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert len(initial_diagnostics.diagnostics) == 1
        config = json.loads(fixture.config_path.read_text(encoding="utf-8"))
        config["push_diagnostics"] = False
        fixture.config_path.write_text(
            json.dumps(config, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        result = session.definition(
            _anchor(repository, "pkg/service.py", 10, 20),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert result == ProviderLocations((), "not_ready", True)
        fresh = session.definition(
            _anchor(repository, "pkg/service.py", 10, 20),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert fresh.coverage == "provider_reported"
        assert len(fresh.locations) == 1
        assert session.readiness == "query_ready"
        assert session.readiness_evidence == (
            "initialize",
            "initialized",
            "configuration",
            "didOpen",
            "documentSymbol",
        )
        messages = _client_events(fixture.events())
        pids = _client_pids(messages)
        assert len(pids) == 2
        for pid in pids:
            _assert_generation_replayed(messages, pid, content)
        assert sum(event["method"] == "textDocument/definition" for event in messages) == 3
        assert session.diagnostics(
            "pkg/service.py",
            deadline=time.monotonic() + 0.1,
        ) == ProviderDiagnostics((), None, True)
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)
    assert tuple((state_root / "run/lsp").iterdir()) == ()


def test_server_mutated_before_transparent_restart_never_spawns_replacement(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
) -> None:
    marker = repository / ".fake-lsp-crashed"
    fixture = create_semantic_pyright_fixture(
        repository,
        config={
            "crash_once_method": "textDocument/definition",
            "crash_marker": str(marker),
        },
    )
    server, identity = _copied_server_identity(repository, fixture)
    session = PyrightSession(
        resolve_repository_scope(repository),
        identity,
        state_root=state_root,
    )
    trees: list[lsp_process.ProcessTree] = []
    real_spawn = lsp_process.ProcessTree._spawn_with_deadline.__func__

    def record_spawn(
        cls: type[lsp_process.ProcessTree],
        command: object,
        *,
        cwd: Path,
        env: object,
        deadline: float,
        pass_fds: object = (),
    ) -> lsp_process.ProcessTree:
        tree = real_spawn(
            cls,
            command,
            cwd=cwd,
            env=env,
            deadline=deadline,
            pass_fds=pass_fds,
        )  # type: ignore[arg-type]
        trees.append(tree)
        return tree

    monkeypatch.setattr(
        lsp_process.ProcessTree,
        "_spawn_with_deadline",
        classmethod(record_spawn),
    )
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + SHORT_TIMEOUT)
        assert session.readiness == "query_ready"
        assert len(trees) == 1
        server.write_bytes(server.read_bytes() + b"\n# changed before restart\n")

        with pytest.raises(ProtocolViolation):
            result = session.definition(
                _anchor(repository, "pkg/service.py", 10, 20),
                deadline=time.monotonic() + SHORT_TIMEOUT,
            )
            assert result.coverage != "provider_reported"

        process = session._process
        assert process is not None
        coordinator = process._coordinator
        assert len(trees) == 1
        assert process.restart_count == 0
        with coordinator.condition:
            assert coordinator.condition.wait_for(
                lambda: coordinator.phase
                is lsp_process.LifecyclePhase.STOPPED_FAILURE,
                timeout=5,
            )
        assert process.state is ProcessState.FAILED
        assert coordinator.active is None
        assert coordinator.candidate is None
        assert session.readiness == "not_ready"
        assert not (process.owner_root / "lease.json").exists()
        assert not lsp_process.coordinator_has_ownership(coordinator)
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_server_mutated_during_replacement_bootstrap_never_commits_candidate(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
) -> None:
    marker = repository / ".fake-lsp-crashed"
    fixture = create_semantic_pyright_fixture(
        repository,
        config={
            "crash_once_method": "textDocument/definition",
            "crash_marker": str(marker),
        },
    )
    server, identity = _copied_server_identity(repository, fixture)
    session = PyrightSession(
        resolve_repository_scope(repository),
        identity,
        state_root=state_root,
    )
    original = server.read_bytes()
    bootstrap_nonces: list[str] = []
    mutation_errors: list[OSError] = []
    trees: list[lsp_process.ProcessTree] = []
    real_bootstrap = session._bootstrap_generation
    real_spawn = lsp_process.ProcessTree._spawn_with_deadline.__func__

    def mutate_during_bootstrap(
        protocol: object,
        process_id: int,
        generation_nonce: str,
        deadline: float,
    ) -> ProcessState:
        state = real_bootstrap(
            protocol,  # type: ignore[arg-type]
            process_id,
            generation_nonce,
            deadline,
        )
        bootstrap_nonces.append(generation_nonce)
        if len(bootstrap_nonces) == 2:
            try:
                server.write_bytes(original + b"\n# changed during restart bootstrap\n")
            except OSError as error:
                mutation_errors.append(error)
                raise
        return state

    def record_spawn(
        cls: type[lsp_process.ProcessTree],
        command: object,
        *,
        cwd: Path,
        env: object,
        deadline: float,
        pass_fds: object = (),
    ) -> lsp_process.ProcessTree:
        tree = real_spawn(
            cls,
            command,
            cwd=cwd,
            env=env,
            deadline=deadline,
            pass_fds=pass_fds,
        )  # type: ignore[arg-type]
        trees.append(tree)
        return tree

    monkeypatch.setattr(session, "_bootstrap_generation", mutate_during_bootstrap)
    monkeypatch.setattr(
        lsp_process.ProcessTree,
        "_spawn_with_deadline",
        classmethod(record_spawn),
    )
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + SHORT_TIMEOUT)
        first_nonce = session._generation_nonce
        assert first_nonce is not None
        assert session.readiness == "query_ready"

        with pytest.raises(ProtocolViolation):
            result = session.definition(
                _anchor(repository, "pkg/service.py", 10, 20),
                deadline=time.monotonic() + SHORT_TIMEOUT,
            )
            assert result.coverage != "provider_reported"

        process = session._process
        assert process is not None
        coordinator = process._coordinator
        assert len(bootstrap_nonces) == 2
        assert bootstrap_nonces[1] != first_nonce
        assert len(trees) == 2
        with coordinator.condition:
            assert coordinator.condition.wait_for(
                lambda: coordinator.phase
                is lsp_process.LifecyclePhase.STOPPED_FAILURE,
                timeout=5,
            )
        assert all(tree.process.poll() is not None for tree in trees)
        assert mutation_errors if os.name == "nt" else mutation_errors == []
        assert process.generation_nonce == first_nonce
        assert process.restart_count == 0
        assert process.state is ProcessState.FAILED
        assert coordinator.active is None
        assert coordinator.candidate is None
        assert session.readiness == "not_ready"
        assert not (process.owner_root / "lease.json").exists()
        assert not lsp_process.coordinator_has_ownership(coordinator)
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_successful_close_releases_all_protocol_derived_session_state(
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"push_progress": True},
    )
    session = _session(repository, state_root, fixture)
    session.open_document("pkg/service.py", deadline=time.monotonic() + SHORT_TIMEOUT)
    assert session.readiness == "query_ready"
    assert session._documents
    assert session._ready_uri_generations
    assert session.progress_events

    session.close(deadline=time.monotonic() + SHORT_TIMEOUT)

    assert session.readiness == "not_ready"
    assert session.readiness_evidence == ()
    assert session.position_encoding is None
    assert dict(session.capabilities) == {}
    assert session.progress_events == ()
    assert session._documents == {}
    assert session._readiness_target_uri is None
    assert session._generation_nonce is None
    assert session._ready_uri_generations == {}
    assert session._diagnostics == {}


def test_post_start_probe_promotes_owned_process_state(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + SHORT_TIMEOUT)

        assert session._process is not None
        assert session._process.state is ProcessState.WORKSPACE_READY
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_rejected_lifecycle_promotion_cannot_claim_query_readiness(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    transitions: list[str] = []

    def reject_transition(
        self: LspProcess,
        *,
        generation_nonce: str,
        deadline: float,
    ) -> bool:
        assert self is session._process
        assert deadline > time.monotonic()
        transitions.append(generation_nonce)
        return False

    try:
        session.start(deadline=time.monotonic() + SHORT_TIMEOUT)
        process = session._process
        assert process is not None
        monkeypatch.setattr(LspProcess, "promote_workspace_ready", reject_transition)

        session.open_document("pkg/service.py", deadline=time.monotonic() + SHORT_TIMEOUT)

        assert transitions == [process.generation_nonce]
        assert session.readiness == "protocol_initialized"
        assert process.state is ProcessState.PROTOCOL_INITIALIZED
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_timed_out_lifecycle_promotion_cannot_leave_query_readiness(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)

    def timeout_transition(
        self: LspProcess,
        *,
        generation_nonce: str,
        deadline: float,
    ) -> bool:
        assert self is session._process
        assert generation_nonce == self.generation_nonce
        assert deadline > 0
        raise TimeoutError("lifecycle promotion deadline expired")

    try:
        session.start(deadline=time.monotonic() + 10)
        process = session._process
        assert process is not None
        monkeypatch.setattr(
            LspProcess,
            "promote_workspace_ready",
            timeout_transition,
        )

        with pytest.raises(TimeoutError, match="promotion deadline"):
            session.open_document(
                "pkg/service.py",
                deadline=time.monotonic() + 10,
            )

        assert session.readiness == "protocol_initialized"
        assert session.readiness_evidence == (
            "initialize",
            "initialized",
            "configuration",
            "didOpen",
        )
        assert not session._document_ready_locked(
            (repository / "pkg/service.py").resolve().as_uri()
        )
        assert process.state is ProcessState.PROTOCOL_INITIALIZED
    finally:
        session.close(deadline=time.monotonic() + 5)


# FAILED is no longer terminal for the session: the dead process is replaced
# (audit B-40, tests/test_a_failed_server_is_started_again.py).
@pytest.mark.parametrize(
    "terminal_state",
    [ProcessState.DEGRADED],
)
def test_terminal_process_state_revokes_session_readiness_before_semantic_use(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    terminal_state: ProcessState,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    process: LspProcess | None = None
    original_state: ProcessState | None = None
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + SHORT_TIMEOUT)
        process = session._process
        assert process is not None
        original_state = process.state
        process.state = terminal_state
        definitions_before = _definition_requests(semantic_pyright)

        assert session.readiness == "not_ready"
        assert session.readiness_evidence == ()
        assert session.position_encoding is None
        assert dict(session.capabilities) == {}
        assert session.definition(
            _anchor(repository, "pkg/service.py", 10, 20),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        ) == ProviderLocations((), "not_ready", True)
        assert _definition_requests(semantic_pyright) == definitions_before
    finally:
        if process is not None and original_state is not None:
            process.state = original_state
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_active_operation_and_lru_state_are_released_after_blocking_request(
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"response_delays": {"textDocument/definition": 0.3}},
    )
    session = _session(repository, state_root, fixture)
    anchor = _anchor(repository, "pkg/service.py", 10, 20)
    results: list[ProviderLocations] = []
    errors: list[BaseException] = []

    def query() -> None:
        try:
            results.append(
                session.definition(anchor, deadline=time.monotonic() + SHORT_TIMEOUT)
            )
        except BaseException as error:
            errors.append(error)

    before = session.last_used_monotonic
    thread = threading.Thread(target=query)
    thread.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if any(
            event.get("method") == "textDocument/definition"
            for event in fixture.events()
        ):
            break
        time.sleep(0.01)
    try:
        assert thread.is_alive()
        assert session.active_operations == 1
        observed = time.monotonic()
        assert session.readiness == "query_ready"
        assert time.monotonic() - observed < 0.1
        assert session.last_used_monotonic >= before
        thread.join(SHORT_TIMEOUT)
        assert not thread.is_alive()
        assert errors == []
        assert len(results) == 1
        assert session.active_operations == 0
        assert session.last_used_monotonic >= before

        invalid = SourceAnchor(anchor.path, anchor.line, anchor.utf8_character, 0)
        with pytest.raises(ValueError):
            session.definition(invalid, deadline=time.monotonic() + SHORT_TIMEOUT)
        assert session.active_operations == 0
    finally:
        thread.join(SHORT_TIMEOUT)
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_definition_drops_response_after_same_generation_document_change(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    response_ready = threading.Event()
    release_response = threading.Event()
    results: list[ProviderLocations] = []
    errors: list[BaseException] = []
    worker: threading.Thread | None = None
    try:
        document = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        )
        process = session._process
        generation = session._generation_nonce
        assert process is not None and generation is not None
        request = LspProcess.request

        def block_completed_definition(
            current: LspProcess,
            method: str,
            params: object,
            *,
            deadline: float,
            cancellation: object = None,
        ) -> object:
            result = request(
                current,
                method,
                params,
                deadline=deadline,
                cancellation=cancellation,  # type: ignore[arg-type]
            )
            if current is process and method == "textDocument/definition":
                response_ready.set()
                if not release_response.wait(max(0.0, deadline - time.monotonic())):
                    raise TimeoutError("definition publication barrier expired")
            return result

        monkeypatch.setattr(LspProcess, "request", block_completed_definition)
        anchor = _anchor(repository, "pkg/service.py", 10, 20)

        def query() -> None:
            try:
                results.append(
                    session.definition(anchor, deadline=time.monotonic() + 10)
                )
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=query)
        worker.start()
        assert response_ready.wait(SHORT_TIMEOUT), errors

        (repository / "pkg/service.py").write_bytes(
            document.content + b"\nchanged = True\n"
        )
        revision = compute_workspace_revision(
            resolve_repository_scope(repository),
            deadline=time.monotonic() + 10,
        )
        delta = session.synchronize(revision, deadline=time.monotonic() + 10)
        replacement = session._documents[document.source.uri]
        assert delta.changed == ("pkg/service.py",)
        assert replacement is not document
        assert replacement.version == 2
        assert session._generation_nonce == generation

        release_response.set()
        worker.join(SHORT_TIMEOUT)

        assert not worker.is_alive()
        assert errors == []
        assert results == [ProviderLocations((), "not_ready", True)]
    finally:
        release_response.set()
        if worker is not None:
            worker.join(SHORT_TIMEOUT)
        session.close(deadline=time.monotonic() + 5)


_SYNCHRONIZE_FENCE_CASES = (
    ("definition", "textDocument/definition"),
    ("references", "textDocument/references"),
    ("implementations", "textDocument/implementation"),
    ("type_definition", "textDocument/typeDefinition"),
    ("document_symbols", "textDocument/documentSymbol"),
    ("hover", "textDocument/hover"),
    ("incoming_calls", "textDocument/prepareCallHierarchy"),
    ("workspace_symbols", "workspace/symbol"),
)


def _synchronize_fence_query(
    session: PyrightSession,
    repository: Path,
    feature: str,
    *,
    deadline: float,
) -> object:
    if feature == "document_symbols":
        return session.document_symbols("pkg/service.py", deadline=deadline)
    if feature == "workspace_symbols":
        return session.workspace_symbols("Service", deadline=deadline)
    anchor = _anchor(repository, "pkg/service.py", 10, 20)
    return getattr(session, feature)(anchor, deadline=deadline)


def _synchronize_fence_not_ready(feature: str) -> object:
    if feature == "hover":
        return ProviderHover(None, None, True)
    if feature == "incoming_calls":
        return ProviderCalls("incoming", (), "not_ready", True)
    return ProviderLocations((), "not_ready", True)


@pytest.mark.parametrize(
    ("feature", "request_method"),
    _SYNCHRONIZE_FENCE_CASES,
)
def test_synchronize_fence_rejects_queries_started_before_and_during_wire_commit(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    feature: str,
    request_method: str,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={
            "capabilities": {
                "callHierarchyProvider": True,
                "definitionProvider": True,
                "documentSymbolProvider": True,
                "hoverProvider": True,
                "implementationProvider": True,
                "referencesProvider": True,
                "textDocumentSync": 2,
                "typeDefinitionProvider": True,
                "workspaceSymbolProvider": True,
            }
        },
    )
    session = _session(repository, state_root, fixture)
    first_response_ready = threading.Event()
    release_first_response = threading.Event()
    wire_delivered = threading.Event()
    release_commit = threading.Event()
    first_done = threading.Event()
    during_done = threading.Event()
    first_results: list[object] = []
    during_results: list[object] = []
    sync_results: list[WorkspaceDelta] = []
    errors: list[BaseException] = []
    semantic_requests = 0
    first_worker: threading.Thread | None = None
    during_worker: threading.Thread | None = None
    sync_worker: threading.Thread | None = None
    try:
        document = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        scope = resolve_repository_scope(repository)
        prior = compute_workspace_revision(scope, deadline=time.monotonic() + SHORT_TIMEOUT)
        session.synchronize(prior, deadline=time.monotonic() + SHORT_TIMEOUT)
        process = session._process
        assert process is not None
        request = LspProcess.request
        notify_generation = LspProcess.notify_generation
        start = PyrightSession.start

        def reject_start_inside_active_fence(
            current: PyrightSession,
            *,
            deadline: float,
        ) -> None:
            if current is session and threading.current_thread().name == (
                f"during-synchronize-{feature}"
            ):
                raise AssertionError("semantic fence was checked after startup work")
            start(current, deadline=deadline)

        def block_first_completed_response(
            current: LspProcess,
            method: str,
            params: object,
            *,
            deadline: float,
            cancellation: object = None,
        ) -> object:
            nonlocal semantic_requests
            result = request(
                current,
                method,
                params,
                deadline=deadline,
                cancellation=cancellation,  # type: ignore[arg-type]
            )
            if current is process and method == request_method:
                semantic_requests += 1
                _hold_first_response(
                    semantic_requests,
                    first_response_ready,
                    release_first_response,
                    deadline,
                )
            return result

        def hold_after_delivered_change(
            current: LspProcess,
            method: str,
            params: object,
            *,
            generation_nonce: str,
            deadline: float,
        ) -> bool:
            delivered = notify_generation(
                current,
                method,
                params,
                generation_nonce=generation_nonce,
                deadline=deadline,
            )
            if current is process and method == "textDocument/didChange":
                assert delivered is True
                wire_delivered.set()
                if not release_commit.wait(max(0.0, deadline - time.monotonic())):
                    raise TimeoutError("synchronize commit release expired")
            return delivered

        monkeypatch.setattr(LspProcess, "request", block_first_completed_response)
        monkeypatch.setattr(LspProcess, "notify_generation", hold_after_delivered_change)
        monkeypatch.setattr(PyrightSession, "start", reject_start_inside_active_fence)

        def query(results: list[object], done: threading.Event) -> None:
            try:
                results.append(
                    _synchronize_fence_query(
                        session,
                        repository,
                        feature,
                        deadline=time.monotonic() + SHORT_TIMEOUT,
                    )
                )
            except BaseException as error:
                errors.append(error)
            finally:
                done.set()

        first_worker = threading.Thread(
            target=query,
            args=(first_results, first_done),
            name=f"pre-synchronize-{feature}",
        )
        first_worker.start()
        assert first_response_ready.wait(SHORT_TIMEOUT), errors

        (repository / "pkg/service.py").write_bytes(
            document.content + b"\nchanged = True\n"
        )
        revision = compute_workspace_revision(scope, deadline=time.monotonic() + SHORT_TIMEOUT)

        def synchronize() -> None:
            try:
                sync_results.append(
                    session.synchronize(revision, deadline=time.monotonic() + SHORT_TIMEOUT)
                )
            except BaseException as error:
                errors.append(error)

        sync_worker = threading.Thread(
            target=synchronize,
            name=f"synchronize-{feature}",
        )
        sync_worker.start()
        assert wire_delivered.wait(SHORT_TIMEOUT), errors
        assert session._documents[document.source.uri] is document

        during_worker = threading.Thread(
            target=query,
            args=(during_results, during_done),
            name=f"during-synchronize-{feature}",
        )
        during_worker.start()
        assert during_done.wait(SHORT_TIMEOUT), "semantic query waited for synchronize commit"
        assert errors == []
        not_ready = _synchronize_fence_not_ready(feature)
        assert during_results == [not_ready]
        assert semantic_requests == 1

        release_first_response.set()
        assert first_done.wait(SHORT_TIMEOUT), errors
        assert first_results == [not_ready]
        assert semantic_requests == 1

        release_commit.set()
        sync_worker.join(SHORT_TIMEOUT)
        assert not sync_worker.is_alive()
        assert errors == []
        assert len(sync_results) == 1
        assert session._documents[document.source.uri] is not document

        fresh = _synchronize_fence_query(
            session,
            repository,
            feature,
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert fresh != not_ready
        assert semantic_requests >= 2
    finally:
        release_first_response.set()
        release_commit.set()
        _join_started_workers((first_worker, during_worker, sync_worker))
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def _join_started_workers(workers: tuple[threading.Thread | None, ...]) -> None:
    for worker in workers:
        if worker is not None:
            worker.join(SHORT_TIMEOUT)


def test_call_hierarchy_stops_after_prepare_when_document_version_changes(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    prepare_ready = threading.Event()
    release_prepare = threading.Event()
    results: list[ProviderCalls] = []
    errors: list[BaseException] = []
    second_stage_calls = 0
    worker: threading.Thread | None = None
    try:
        document = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        )
        process = session._process
        assert process is not None
        request = LspProcess.request

        def record_stage(method: str, deadline: float) -> None:
            nonlocal second_stage_calls
            if method == "textDocument/prepareCallHierarchy":
                _hold_at_barrier(prepare_ready, release_prepare, deadline)
                return
            if method == "callHierarchy/incomingCalls":
                second_stage_calls += 1

        def block_completed_prepare(
            current: LspProcess,
            method: str,
            params: object,
            *,
            deadline: float,
            cancellation: object = None,
        ) -> object:
            result = request(
                current,
                method,
                params,
                deadline=deadline,
                cancellation=cancellation,  # type: ignore[arg-type]
            )
            if current is not process:
                return result
            record_stage(method, deadline)
            return result

        monkeypatch.setattr(LspProcess, "request", block_completed_prepare)
        anchor = _anchor(repository, "pkg/service.py", 10, 20)

        def query() -> None:
            try:
                results.append(
                    session.incoming_calls(anchor, deadline=time.monotonic() + 10)
                )
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=query)
        worker.start()
        assert prepare_ready.wait(SHORT_TIMEOUT), errors

        (repository / "pkg/service.py").write_bytes(
            document.content + b"\nchanged = True\n"
        )
        revision = compute_workspace_revision(
            resolve_repository_scope(repository),
            deadline=time.monotonic() + 10,
        )
        session.synchronize(revision, deadline=time.monotonic() + 10)
        release_prepare.set()
        worker.join(SHORT_TIMEOUT)

        assert not worker.is_alive()
        assert errors == []
        assert results == [ProviderCalls("incoming", (), "not_ready", True)]
        assert second_stage_calls == 0
    finally:
        release_prepare.set()
        if worker is not None:
            worker.join(SHORT_TIMEOUT)
        session.close(deadline=time.monotonic() + 5)


def test_workspace_symbols_drop_response_after_generation_replacement(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    response_ready = threading.Event()
    release_response = threading.Event()
    results: list[ProviderLocations] = []
    errors: list[BaseException] = []
    worker: threading.Thread | None = None
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + SHORT_TIMEOUT)
        process = session._process
        generation = session._generation_nonce
        assert process is not None and generation is not None
        assert session.readiness == "query_ready"
        request = LspProcess.request

        def block_completed_workspace_symbols(
            current: LspProcess,
            method: str,
            params: object,
            *,
            deadline: float,
            cancellation: object = None,
        ) -> object:
            result = request(
                current,
                method,
                params,
                deadline=deadline,
                cancellation=cancellation,  # type: ignore[arg-type]
            )
            if current is process and method == "workspace/symbol":
                response_ready.set()
                if not release_response.wait(max(0.0, deadline - time.monotonic())):
                    raise TimeoutError("workspace symbol publication barrier expired")
            return result

        monkeypatch.setattr(LspProcess, "request", block_completed_workspace_symbols)

        def query() -> None:
            try:
                results.append(
                    session.workspace_symbols("Service", deadline=time.monotonic() + SHORT_TIMEOUT)
                )
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=query)
        worker.start()
        assert response_ready.wait(SHORT_TIMEOUT), errors

        process.restart(time.monotonic() + 10)
        assert session._process is process
        assert session._generation_nonce == process.generation_nonce
        assert session._generation_nonce != generation
        assert session.readiness == "query_ready"

        release_response.set()
        worker.join(SHORT_TIMEOUT)

        assert not worker.is_alive()
        assert errors == []
        assert results == [ProviderLocations((), "not_ready", True)]
    finally:
        release_response.set()
        if worker is not None:
            worker.join(SHORT_TIMEOUT)
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_diagnostics_wait_stops_when_exact_open_document_is_replaced(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"push_diagnostics": False},
    )
    session = _session(repository, state_root, fixture)
    waiting = threading.Event()
    done = threading.Event()
    results: list[ProviderDiagnostics] = []
    errors: list[BaseException] = []
    worker: threading.Thread | None = None
    real_wait = threading.Condition.wait

    def observe_diagnostics_wait(
        condition: threading.Condition,
        timeout: float | None = None,
    ) -> bool:
        if condition is session._condition and threading.current_thread().name == (
            "stale-diagnostics-wait"
        ):
            waiting.set()
        return real_wait(condition, timeout)

    monkeypatch.setattr(threading.Condition, "wait", observe_diagnostics_wait)
    try:
        document = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )

        def query() -> None:
            try:
                results.append(
                    session.diagnostics(
                        "pkg/service.py",
                        deadline=time.monotonic() + SHORT_TIMEOUT,
                    )
                )
            except BaseException as error:
                errors.append(error)
            finally:
                done.set()

        worker = threading.Thread(target=query, name="stale-diagnostics-wait")
        worker.start()
        assert waiting.wait(SHORT_TIMEOUT), errors

        (repository / "pkg/service.py").write_bytes(
            document.content + b"\nchanged = True\n"
        )
        revision = compute_workspace_revision(
            resolve_repository_scope(repository),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        session.synchronize(revision, deadline=time.monotonic() + SHORT_TIMEOUT)

        assert done.wait(SHORT_TIMEOUT), "diagnostics wait retained a replaced document"
        assert errors == []
        assert results == [ProviderDiagnostics((), None, True)]
    finally:
        if worker is not None:
            worker.join(SHORT_TIMEOUT)
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_configuration_request_item_bound_returns_empty_snapshot(
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={
            "configuration_items": [
                {"section": "python"} for _ in range(65)
            ]
        },
    )
    session = _session(repository, state_root, fixture)
    try:
        session.start(deadline=time.monotonic() + SHORT_TIMEOUT)
        configuration = next(
            event for event in fixture.events() if event["kind"] == "configuration"
        )
        assert configuration["values"] == []
        assert session.readiness == "protocol_initialized"
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_invalid_server_capability_shape_degrades_without_claiming_support(
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"capabilities": {"definitionProvider": "yes"}},
    )
    session = _session(repository, state_root, fixture)

    session.start(deadline=time.monotonic() + SHORT_TIMEOUT)
    assert session.readiness == "not_ready"
    assert session.capabilities == {}
    assert session.degradation_codes == ("pyright_definition_capability_invalid",)


@pytest.mark.parametrize(
    ("repository_value", "identity_value", "state_value", "error"),
    [
        (object(), _missing_identity(), Path("state"), TypeError),
        (None, object(), Path("state"), TypeError),
        (None, _missing_identity(), "state", TypeError),
    ],
)
def test_constructor_requires_exact_contract_types(
    repository: Path,
    repository_value: object,
    identity_value: object,
    state_value: object,
    error: type[Exception],
) -> None:
    scope = resolve_repository_scope(repository)
    if repository_value is None:
        repository_value = scope
    with pytest.raises(error):
        PyrightSession(
            repository_value,  # type: ignore[arg-type]
            identity_value,  # type: ignore[arg-type]
            state_root=state_value,  # type: ignore[arg-type]
        )


def test_qualified_identity_validation_unwinds_start_state_for_retry_and_close(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    invalid = replace(semantic_pyright.identity, node_executable=None)
    session = PyrightSession(
        resolve_repository_scope(repository),
        invalid,  # type: ignore[arg-type]
        state_root=state_root,
    )

    # An unusable install is a named degradation, not an exception that re-arms
    # the start for every query (audit B-39,
    # docs/research/2026-09-25-a-broken-install-is-a-named-degradation.md).
    session.start(deadline=time.monotonic() + SHORT_TIMEOUT)
    first = (session.active_operations, session.readiness, session.degradation_codes)
    session.start(deadline=time.monotonic() + SHORT_TIMEOUT)
    assert first == (0, "not_ready", ("pyright_install_invalid",))
    assert session.degradation_codes == ("pyright_install_invalid",)
    session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_symlinked_lsp_owner_parent_fails_closed_without_mutating_target(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    outside = state_root.parent / "outside-lsp"
    outside.mkdir()
    lsp_parent = state_root / "run/lsp"
    lsp_parent.parent.mkdir(exist_ok=True)
    try:
        lsp_parent.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    session = _session(repository, state_root, semantic_pyright)

    session.start(deadline=time.monotonic() + SHORT_TIMEOUT)
    assert session.readiness == "not_ready"
    assert session.degradation_codes == ("pyright_startup_failed",)
    assert tuple(outside.iterdir()) == ()

    outside_state_parent = state_root.parent / "outside-state-parent"
    linked_state = outside_state_parent / "state"
    linked_state.mkdir(parents=True)
    state_parent_link = state_root.parent / "state-parent-link"
    state_parent_link.symlink_to(outside_state_parent, target_is_directory=True)
    linked_session = _session(
        repository,
        state_parent_link / "state",
        semantic_pyright,
    )
    try:
        linked_session.start(deadline=time.monotonic() + SHORT_TIMEOUT)
        assert linked_session.readiness == "not_ready"
        assert linked_session.degradation_codes == ("pyright_startup_failed",)
        assert tuple(linked_state.iterdir()) == ()
    finally:
        linked_session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_exact_public_method_annotations() -> None:
    assert get_type_hints(PyrightSession.start) == {
        "deadline": float,
        "return": type(None),
    }
    assert get_type_hints(PyrightSession.open_document) == {
        "path": str,
        "deadline": float,
        "return": OpenDocument,
    }
    for name in ("definition", "references", "implementations", "type_definition"):
        assert get_type_hints(getattr(PyrightSession, name)) == {
            "anchor": SourceAnchor,
            "deadline": float,
            "return": ProviderLocations,
        }
    assert get_type_hints(PyrightSession.hover) == {
        "anchor": SourceAnchor,
        "deadline": float,
        "return": ProviderHover,
    }
    for name in ("incoming_calls", "outgoing_calls"):
        assert get_type_hints(getattr(PyrightSession, name)) == {
            "anchor": SourceAnchor,
            "deadline": float,
            "return": ProviderCalls,
        }
    assert get_type_hints(PyrightSession.document_symbols) == {
        "path": str,
        "deadline": float,
        "return": ProviderLocations,
    }
    assert get_type_hints(PyrightSession.workspace_symbols) == {
        "query": str,
        "deadline": float,
        "return": ProviderLocations,
    }
    assert get_type_hints(PyrightSession.diagnostics) == {
        "path": str,
        "deadline": float,
        "return": ProviderDiagnostics,
    }
    assert get_type_hints(PyrightSession.close) == {
        "deadline": float,
        "return": type(None),
    }


def test_unqualified_semantic_features_return_not_ready_without_opening_or_wiring(
    repository: Path,
    state_root: Path,
) -> None:
    session = PyrightSession(
        resolve_repository_scope(repository),
        _missing_identity(),
        state_root=state_root,
    )
    anchor = _anchor(repository, "pkg/service.py", 10, 20)

    assert session.definition(
        anchor,
        deadline=time.monotonic() + SHORT_TIMEOUT,
    ) == ProviderLocations((), "not_ready", True)
    assert session.hover(
        anchor,
        deadline=time.monotonic() + SHORT_TIMEOUT,
    ) == ProviderHover(None, None, True)
    assert session.incoming_calls(
        anchor,
        deadline=time.monotonic() + SHORT_TIMEOUT,
    ) == ProviderCalls("incoming", (), "not_ready", True)
    assert session.document_symbols(
        "pkg/service.py",
        deadline=time.monotonic() + SHORT_TIMEOUT,
    ) == ProviderLocations((), "not_ready", True)
    assert session.workspace_symbols(
        "Service",
        deadline=time.monotonic() + SHORT_TIMEOUT,
    ) == ProviderLocations((), "not_ready", True)
    assert session.diagnostics(
        "pkg/service.py",
        deadline=time.monotonic() + SHORT_TIMEOUT,
    ) == ProviderDiagnostics((), None, True)
    assert not (state_root / "run/lsp").exists()


def test_non_string_position_encoding_shape_is_operational_degradation(
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"capabilities": {"positionEncoding": ["utf-8"]}},
    )
    session = _session(repository, state_root, fixture)

    session.start(deadline=time.monotonic() + SHORT_TIMEOUT)
    assert session.readiness == "not_ready"
    assert session.degradation_codes == ("pyright_position_encoding_unsupported",)


def test_supported_empty_locations_are_complete_only_after_query_readiness(
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={
            "responses": {
                "textDocument/definition": None,
                "textDocument/references": None,
            }
        },
    )
    session = _session(repository, state_root, fixture)
    anchor = _anchor(repository, "pkg/service.py", 10, 20)
    try:
        assert session.definition(
            anchor,
            deadline=time.monotonic() + SHORT_TIMEOUT,
        ) == ProviderLocations((), "provider_reported", False)
        assert session.references(
            anchor,
            deadline=time.monotonic() + SHORT_TIMEOUT,
        ) == ProviderLocations((), "provider_reported", True)
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)
