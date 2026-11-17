"""Workspace synchronization of the pinned Pyright session: revisions, notifications and recovery."""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
from llm_wiki import (
    pyright_session_diagnostics,
    pyright_session_documents,
)
from llm_wiki.pyright_session_documents import MAX_DOCUMENT_BYTES
from lsp_process import LspProcess
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
    _session,
    semantic_pyright,  # noqa: F401
)
from tests.slow_machine import SHORT_TIMEOUT


def _oversized_entry(entry):
    if entry.path != "pkg/service.py":
        return entry
    return replace(entry, size=MAX_DOCUMENT_BYTES + 1)


def _oversized_document_revision(revision):
    """Claim the changed document exceeds the per-document byte limit."""
    return replace(
        revision, entries=tuple(_oversized_entry(entry) for entry in revision.entries)
    )


def _lower_open_document_limit(monkeypatch, revision) -> None:
    """Put the aggregate open-document limit just under the changed document."""
    entry = next(item for item in revision.entries if item.path == "pkg/service.py")
    monkeypatch.setattr(
        pyright_session_documents, "MAX_OPEN_DOCUMENT_BYTES", entry.size - 1
    )


def _client_messages(fixture: SemanticPyrightFixture, method: str) -> tuple[dict, ...]:
    return tuple(
        event["message"]
        for event in fixture.events()
        if event.get("kind") == "client-message" and event.get("method") == method
    )


def _wait_client_messages(
    fixture: SemanticPyrightFixture, method: str, *, count: int = 1
) -> tuple[dict, ...]:
    deadline = time.monotonic() + 5
    while True:
        messages = _client_messages(fixture, method)
        if len(messages) >= count or time.monotonic() >= deadline:
            return messages
        time.sleep(0.02)


def test_first_synchronize_establishes_retained_workspace_revision(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + SHORT_TIMEOUT)
        scope = resolve_repository_scope(repository)
        revision = compute_workspace_revision(scope, deadline=time.monotonic() + SHORT_TIMEOUT)
        delta = session.synchronize(revision, deadline=time.monotonic() + SHORT_TIMEOUT)
        assert delta == WorkspaceDelta((), (), (), ())
        assert session._workspace_revision is revision
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_first_synchronize_reverifies_unchanged_open_document_after_revision(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        document = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        )
        revision = compute_workspace_revision(
            resolve_repository_scope(repository),
            deadline=time.monotonic() + 10,
        )
        (repository / "pkg/service.py").write_bytes(b"mutated_after_revision = True\n")
        before_events = tuple(semantic_pyright.events())

        with pytest.raises(RuntimeError, match="hash differs from the revision"):
            session.synchronize(revision, deadline=time.monotonic() + 10)

        assert tuple(semantic_pyright.events()) == before_events
        assert session._documents[document.source.uri] is document
        assert session._workspace_revision is None
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_every_synchronize_reverifies_unchanged_retained_document_before_wire(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        document = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        )
        scope = resolve_repository_scope(repository)
        prior = compute_workspace_revision(scope, deadline=time.monotonic() + 10)
        session.synchronize(prior, deadline=time.monotonic() + 10)
        unchanged = compute_workspace_revision(scope, deadline=time.monotonic() + 10)
        before_events = tuple(semantic_pyright.events())
        before_document_bytes = session._document_bytes
        (repository / "pkg/service.py").write_bytes(b"mutated_after_snapshot = True\n")

        with pytest.raises(RuntimeError, match="hash differs from the revision"):
            session.synchronize(unchanged, deadline=time.monotonic() + 10)

        assert tuple(semantic_pyright.events()) == before_events
        assert session._documents[document.source.uri] is document
        assert session._document_bytes == before_document_bytes
        assert session._workspace_revision is prior
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_first_synchronize_reconciles_changed_and_absent_open_documents(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    absent_path = repository / "pkg/absent.py"
    absent_path.write_bytes(b"absent = True\n")
    session = _session(repository, state_root, semantic_pyright)
    try:
        changed = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        )
        absent = session.open_document(
            "pkg/absent.py",
            deadline=time.monotonic() + 10,
        )
        (repository / "pkg/service.py").write_bytes(b"changed = True\n")
        absent_path.unlink()
        revision = compute_workspace_revision(
            resolve_repository_scope(repository),
            deadline=time.monotonic() + 10,
        )

        delta = session.synchronize(revision, deadline=time.monotonic() + 10)

        assert delta.changed == ("pkg/service.py",)
        assert delta.deleted == ("pkg/absent.py",)
        changes = _wait_client_messages(semantic_pyright, "textDocument/didChange")
        closes = _wait_client_messages(semantic_pyright, "textDocument/didClose")
        assert len(changes) == 1
        assert len(closes) == 1
        assert session._documents[changed.source.uri].content == b"changed = True\n"
        assert absent.source.uri not in session._documents
        assert session._workspace_revision is revision
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_first_synchronize_rejects_stale_changed_document_revision(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        document = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        )
        (repository / "pkg/service.py").write_bytes(b"first = True\n")
        revision = compute_workspace_revision(
            resolve_repository_scope(repository),
            deadline=time.monotonic() + 10,
        )
        (repository / "pkg/service.py").write_bytes(b"second = True\n")

        with pytest.raises(RuntimeError, match="hash differs from the revision"):
            session.synchronize(revision, deadline=time.monotonic() + 10)

        assert session._documents[document.source.uri] is document
        assert session._workspace_revision is None
        assert _client_messages(semantic_pyright, "textDocument/didChange") == ()
    finally:
        session.close(deadline=time.monotonic() + 5)


@pytest.mark.parametrize("bound", ["document", "aggregate"])
def test_synchronize_rejects_projected_source_bounds_before_reads_or_notifications(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    bound: str,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        document = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        scope = resolve_repository_scope(repository)
        prior = compute_workspace_revision(scope, deadline=time.monotonic() + SHORT_TIMEOUT)
        session.synchronize(prior, deadline=time.monotonic() + SHORT_TIMEOUT)
        (repository / "pkg/service.py").write_bytes(b"projected = True\n")
        revision = compute_workspace_revision(scope, deadline=time.monotonic() + SHORT_TIMEOUT)
        if bound == "document":
            revision = _oversized_document_revision(revision)
            expected = "source document byte limit"
        else:
            _lower_open_document_limit(monkeypatch, revision)
            expected = "open document source bytes limit"

        reads = 0

        def reject_read(*_args: object, **_kwargs: object) -> bytes:
            nonlocal reads
            reads += 1
            raise AssertionError("changed document was read before projected bounds")

        monkeypatch.setattr(pyright_session_documents, "read_stable_bytes", reject_read)
        before_events = tuple(semantic_pyright.events())

        with pytest.raises(RuntimeError, match=expected):
            session.synchronize(revision, deadline=time.monotonic() + SHORT_TIMEOUT)

        assert reads == 0
        assert tuple(semantic_pyright.events()) == before_events
        assert session._documents[document.source.uri] is document
        assert session._workspace_revision is prior
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_changed_open_document_sends_full_did_change_and_increments_version_once(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        document = session.open_document(
            "pkg/service.py", deadline=time.monotonic() + 10
        )
        assert document.version == 1
        scope = resolve_repository_scope(repository)
        session.synchronize(
            compute_workspace_revision(scope, deadline=time.monotonic() + 10),
            deadline=time.monotonic() + 10,
        )
        (repository / "pkg/service.py").write_bytes(
            b"class Updated:\n    pass\n"
        )
        delta = session.synchronize(
            compute_workspace_revision(scope, deadline=time.monotonic() + 10),
            deadline=time.monotonic() + 10,
        )
        assert delta.changed == ("pkg/service.py",)
        changes = _wait_client_messages(semantic_pyright, "textDocument/didChange")
        assert len(changes) == 1
        body = changes[0]["params"]
        assert body["textDocument"]["version"] == 2
        assert body["contentChanges"] == [{"text": "class Updated:\n    pass\n"}]
        assert session._documents[document.source.uri].version == 2
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_synchronize_proves_retained_document_open_before_did_change(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    sent_methods: list[str] = []
    recoveries: list[tuple[LspProcess, str]] = []
    try:
        session.start(deadline=time.monotonic() + 10)
        process = session._process
        assert process is not None
        notify_generation = LspProcess.notify_generation

        def reject_did_open(
            current: LspProcess,
            method: str,
            params: object,
            *,
            generation_nonce: str,
            deadline: float,
        ) -> bool:
            if current is process:
                sent_methods.append(method)
                if method == "textDocument/didOpen":
                    return False
            return notify_generation(
                current,
                method,
                params,
                generation_nonce=generation_nonce,
                deadline=deadline,
            )

        monkeypatch.setattr(LspProcess, "notify_generation", reject_did_open)
        document = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        )
        generation = session._generation_nonce
        assert generation is not None
        assert not session._wire_document_opened(document, generation)
        sent_methods.clear()

        def record_recovery(
            current: LspProcess,
            failed_generation: str,
            *,
            deadline: float,
        ) -> None:
            assert current is process
            assert failed_generation == generation
            assert time.monotonic() < deadline
            recoveries.append((current, failed_generation))

        monkeypatch.setattr(
            session,
            "_recover_synchronize_snapshot",
            record_recovery,
        )
        (repository / "pkg/service.py").write_bytes(b"changed = True\n")
        revision = compute_workspace_revision(
            resolve_repository_scope(repository),
            deadline=time.monotonic() + 10,
        )

        with pytest.raises(RuntimeError, match="didOpen"):
            session.synchronize(revision, deadline=time.monotonic() + 10)

        assert sent_methods == ["textDocument/didOpen"]
        assert recoveries == [(process, generation)]
        assert session._documents[document.source.uri] is document
        assert session._workspace_revision is None
        assert not session._wire_document_opened(document, generation)
        with session._lock:
            assert not session._document_ready_locked(document.source.uri)
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_synchronize_commit_preserves_diagnostics_published_during_wire_calls(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"push_diagnostics": False},
    )
    session = _session(repository, state_root, fixture)
    wire_entered = threading.Event()
    release_wire = threading.Event()
    sync_errors: list[BaseException] = []
    sync_results: list[WorkspaceDelta] = []
    worker: threading.Thread | None = None
    base = {
        "range": {
            "start": {"line": 0, "character": 0},
            "end": {"line": 0, "character": 1},
        },
    }
    try:
        service = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        api = session.open_document(
            "pkg/api.py",
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        scope = resolve_repository_scope(repository)
        prior = compute_workspace_revision(scope, deadline=time.monotonic() + SHORT_TIMEOUT)
        session.synchronize(prior, deadline=time.monotonic() + SHORT_TIMEOUT)
        session._publish_diagnostics(
            {
                "uri": api.source.uri,
                "version": 1,
                "diagnostics": [{**base, "message": "retained api diagnostic"}],
            }
        )
        (repository / "pkg/service.py").write_bytes(b"changed = True\n")
        revision = compute_workspace_revision(scope, deadline=time.monotonic() + SHORT_TIMEOUT)
        process = session._process
        assert process is not None

        notify_generation = LspProcess.notify_generation

        def block_change_notification(
            current: LspProcess,
            method: str,
            params: object,
            *,
            generation_nonce: str,
            deadline: float,
        ) -> bool:
            if current is process and method == "textDocument/didChange":
                assert generation_nonce == process.generation_nonce
                assert time.monotonic() < deadline
                wire_entered.set()
                if not release_wire.wait(max(0.0, deadline - time.monotonic())):
                    raise TimeoutError("interleaved diagnostics release expired")
                return True
            return notify_generation(
                current,
                method,
                params,
                generation_nonce=generation_nonce,
                deadline=deadline,
            )

        monkeypatch.setattr(LspProcess, "notify_generation", block_change_notification)

        def synchronize() -> None:
            try:
                sync_results.append(
                    session.synchronize(revision, deadline=time.monotonic() + SHORT_TIMEOUT)
                )
            except BaseException as error:
                sync_errors.append(error)

        worker = threading.Thread(target=synchronize)
        worker.start()
        assert wire_entered.wait(SHORT_TIMEOUT)
        session._publish_diagnostics(
            {
                "uri": service.source.uri,
                "version": 2,
                "diagnostics": [
                    {**base, "message": "published between snapshot and commit"}
                ],
            }
        )
        release_wire.set()
        worker.join(SHORT_TIMEOUT)

        assert not worker.is_alive()
        assert sync_errors == []
        assert len(sync_results) == 1
        assert set(session._diagnostics) == {service.source.uri, api.source.uri}
        assert session._diagnostics[service.source.uri].document_version == 2
        assert session._diagnostic_bytes == sum(
            snapshot.retained_bytes for snapshot in session._diagnostics.values()
        )
        assert session._diagnostic_bytes <= pyright_session_diagnostics.MAX_DIAGNOSTIC_BYTES
    finally:
        release_wire.set()
        if worker is not None:
            worker.join(SHORT_TIMEOUT)
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


@pytest.mark.parametrize(
    ("failed_method", "failure_mode"),
    [
        ("textDocument/didChange", "false"),
        ("textDocument/didClose", "exception"),
        ("workspace/didChangeWatchedFiles", "false"),
    ],
)
def test_synchronize_notification_failure_replays_prior_snapshot_without_commit(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    failed_method: str,
    failure_mode: str,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + SHORT_TIMEOUT)
        scope = resolve_repository_scope(repository)
        prior = compute_workspace_revision(scope, deadline=time.monotonic() + SHORT_TIMEOUT)
        session.synchronize(prior, deadline=time.monotonic() + SHORT_TIMEOUT)
        process = session._process
        assert process is not None
        before_documents = dict(session._documents)
        before_document_bytes = session._document_bytes

        def change_documents():
            if failed_method == "textDocument/didChange":
                (repository / "pkg/service.py").write_bytes(b"changed = True\n")
                return
            if failed_method == "textDocument/didClose":
                (repository / "pkg/service.py").unlink()
                return
            (repository / "pkg/watched.py").write_bytes(b"watched = True\n")

        change_documents()
        revision = compute_workspace_revision(scope, deadline=time.monotonic() + SHORT_TIMEOUT)

        notify_generation = LspProcess.notify_generation
        notify = LspProcess.notify

        def fail_generation(
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
            if current is not process or method != failed_method:
                return delivered
            if failure_mode == "exception":
                raise RuntimeError("injected notification failure")
            return False

        def fail_notify(
            current: LspProcess,
            method: str,
            params: object,
            *,
            deadline: float,
        ) -> object:
            delivered = notify(current, method, params, deadline=deadline)
            if current is not process or method != failed_method:
                return delivered
            if failure_mode == "exception":
                raise RuntimeError("injected notification failure")
            return False

        monkeypatch.setattr(LspProcess, "notify_generation", fail_generation)
        monkeypatch.setattr(LspProcess, "notify", fail_notify)

        with pytest.raises(RuntimeError, match="notification"):
            session.synchronize(revision, deadline=time.monotonic() + SHORT_TIMEOUT)

        assert session._workspace_revision is prior
        assert session._documents == before_documents
        assert session._document_bytes == before_document_bytes
        assert process.restart_count == 1
        generation = session._generation_nonce
        assert generation == process.generation_nonce
        assert all(
            session._wire_document_opened(document, generation)
            for document in before_documents.values()
        )
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_synchronize_recovery_preserves_already_replayed_replacement_state(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        document = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        process = session._process
        failed_generation = session._generation_nonce
        assert process is not None and failed_generation is not None

        process.restart(time.monotonic() + 10)
        replacement_generation = session._generation_nonce
        assert replacement_generation == process.generation_nonce
        assert replacement_generation != failed_generation
        assert session.readiness == "query_ready"
        session._publish_diagnostics(
            {
                "uri": document.source.uri,
                "version": document.version,
                "diagnostics": [],
            }
        )
        with session._lock:
            readiness = session._readiness
            evidence = session._readiness_evidence
            ready_uris = dict(session._ready_uri_generations)
            diagnostics = dict(session._diagnostics)
            diagnostic_bytes = session._diagnostic_bytes
            assert session._synchronize_snapshot_replayed_locked(process)

        def forbidden_restart(_process: LspProcess, _deadline: float) -> None:
            pytest.fail("already replayed replacement generation restarted again")

        monkeypatch.setattr(LspProcess, "restart", forbidden_restart)

        session._recover_synchronize_snapshot(
            process,
            failed_generation,
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )

        with session._lock:
            assert session._readiness == readiness == "query_ready"
            assert session._readiness_evidence == evidence
            assert session._ready_uri_generations == ready_uris
            assert session._diagnostics == diagnostics
            assert session._diagnostic_bytes == diagnostic_bytes
            assert session._generation_nonce == replacement_generation
            assert session._process is process
            assert session._synchronize_snapshot_replayed_locked(process)
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_synchronize_deadline_recovery_quarantines_partially_mutated_process(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    partial_changes: list[str] = []
    recovery_deadlines: list[float] = []
    stale_fresh_calls: list[str] = []
    failure_returned = False
    try:
        document = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        )
        scope = resolve_repository_scope(repository)
        prior = compute_workspace_revision(scope, deadline=time.monotonic() + 10)
        session.synchronize(prior, deadline=time.monotonic() + 10)
        process = session._process
        generation = session._generation_nonce
        assert process is not None and generation is not None
        (repository / "pkg/service.py").write_bytes(b"changed = True\n")
        (repository / "pkg/watched.py").write_bytes(b"watched = True\n")
        revision = compute_workspace_revision(scope, deadline=time.monotonic() + 10)
        notify_generation = LspProcess.notify_generation
        restart = LspProcess.restart

        def refuse_after_failure(method: str) -> None:
            if failure_returned:
                stale_fresh_calls.append(method)
                raise AssertionError("fresh work reached quarantined process")

        def partial_delivery(method: str, deliver, generation_nonce: str) -> bool:
            if method == "textDocument/didChange":
                assert deliver() is True
                partial_changes.append(generation_nonce)
                return True
            if method == "workspace/didChangeWatchedFiles":
                assert partial_changes == [generation]
                return False
            return deliver()

        def fail_after_partial_change(
            current: LspProcess,
            method: str,
            params: object,
            *,
            generation_nonce: str,
            deadline: float,
        ) -> bool:
            def deliver() -> bool:
                return notify_generation(
                    current,
                    method,
                    params,
                    generation_nonce=generation_nonce,
                    deadline=deadline,
                )

            if current is not process:
                return deliver()
            refuse_after_failure(method)
            return partial_delivery(method, deliver, generation_nonce)

        def expire_recovery(current: LspProcess, deadline: float) -> None:
            if current is process:
                assert partial_changes == [generation]
                recovery_deadlines.append(deadline)
                raise TimeoutError("injected synchronization recovery deadline expired")
            restart(current, deadline)

        monkeypatch.setattr(
            LspProcess,
            "notify_generation",
            fail_after_partial_change,
        )
        monkeypatch.setattr(LspProcess, "restart", expire_recovery)

        with pytest.raises(RuntimeError, match="notification recovery failed"):
            session.synchronize(revision, deadline=time.monotonic() + 10)
        failure_returned = True

        assert partial_changes == [generation]
        assert len(recovery_deadlines) == 1
        assert session._process is None
        assert session._startup_process is process
        assert session._generation_nonce is None
        assert session.readiness == "not_ready"
        assert session._workspace_revision is prior
        assert session._documents[document.source.uri] is document

        delta = session.synchronize(revision, deadline=time.monotonic() + 10)

        assert delta.changed == ("pkg/service.py",)
        assert session._process is not None and session._process is not process
        assert session._startup_process is None
        assert session._workspace_revision is revision
        assert stale_fresh_calls == []
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_notification_interruption_outranks_later_recovery_interruption(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + 10)
        scope = resolve_repository_scope(repository)
        prior = compute_workspace_revision(scope, deadline=time.monotonic() + 10)
        session.synchronize(prior, deadline=time.monotonic() + 10)
        process = session._process
        assert process is not None
        (repository / "pkg/service.py").write_bytes(b"changed = True\n")
        revision = compute_workspace_revision(scope, deadline=time.monotonic() + 10)
        notify_generation = LspProcess.notify_generation

        def make_notification_interruption() -> KeyboardInterrupt:
            try:
                raise KeyboardInterrupt("original notification interruption")
            except KeyboardInterrupt as error:
                return error

        notification_interruption = make_notification_interruption()
        notification_wrapper = RuntimeError("notification interruption wrapper")
        notification_wrapper.__cause__ = notification_interruption
        recovery_interruption = SystemExit(61)

        def interrupt_notification(
            current: LspProcess,
            method: str,
            params: object,
            *,
            generation_nonce: str,
            deadline: float,
        ) -> bool:
            if current is process and method == "textDocument/didChange":
                raise notification_wrapper
            return notify_generation(
                current,
                method,
                params,
                generation_nonce=generation_nonce,
                deadline=deadline,
            )

        def interrupt_recovery(
            _process: LspProcess,
            _failed_generation: str,
            *,
            deadline: float,
        ) -> None:
            assert time.monotonic() < deadline
            raise recovery_interruption

        monkeypatch.setattr(LspProcess, "notify_generation", interrupt_notification)
        monkeypatch.setattr(
            session,
            "_recover_synchronize_snapshot",
            interrupt_recovery,
        )

        with pytest.raises(
            KeyboardInterrupt,
            match="original notification interruption",
        ) as raised:
            session.synchronize(revision, deadline=time.monotonic() + 10)

        assert raised.value is notification_interruption
        assert raised.value.__cause__ is recovery_interruption
        assert recovery_interruption.__context__ is None
        assert notification_wrapper.__cause__ is notification_interruption
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_wrapped_recovery_interruption_is_not_downgraded_to_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + 10)
        scope = resolve_repository_scope(repository)
        prior = compute_workspace_revision(scope, deadline=time.monotonic() + 10)
        session.synchronize(prior, deadline=time.monotonic() + 10)
        process = session._process
        assert process is not None
        (repository / "pkg/service.py").write_bytes(b"changed = True\n")
        revision = compute_workspace_revision(scope, deadline=time.monotonic() + 10)
        notify_generation = LspProcess.notify_generation
        notification_error = RuntimeError("ordinary notification failure")
        recovery_interruption = SystemExit(67)
        recovery_wrapper = RuntimeError("recovery interruption wrapper")
        recovery_wrapper.__cause__ = recovery_interruption

        def fail_notification(
            current: LspProcess,
            method: str,
            params: object,
            *,
            generation_nonce: str,
            deadline: float,
        ) -> bool:
            if current is process and method == "textDocument/didChange":
                raise notification_error
            return notify_generation(
                current,
                method,
                params,
                generation_nonce=generation_nonce,
                deadline=deadline,
            )

        def wrap_recovery_interruption(
            _process: LspProcess,
            _failed_generation: str,
            *,
            deadline: float,
        ) -> None:
            assert time.monotonic() < deadline
            raise recovery_wrapper

        monkeypatch.setattr(LspProcess, "notify_generation", fail_notification)
        monkeypatch.setattr(
            session,
            "_recover_synchronize_snapshot",
            wrap_recovery_interruption,
        )

        with pytest.raises(SystemExit) as raised:
            session.synchronize(revision, deadline=time.monotonic() + 10)

        assert raised.value is recovery_interruption
        assert raised.value.__cause__ is notification_error
        assert raised.value.__context__ is None
        assert recovery_wrapper.__cause__ is recovery_interruption
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_synchronize_commit_recovery_rethrows_interruption(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + 10)
        scope = resolve_repository_scope(repository)
        prior = compute_workspace_revision(scope, deadline=time.monotonic() + 10)
        session.synchronize(prior, deadline=time.monotonic() + 10)
        (repository / "pkg/service.py").write_bytes(b"changed = True\n")
        revision = compute_workspace_revision(scope, deadline=time.monotonic() + 10)
        process = session._process
        assert process is not None
        notify_generation = LspProcess.notify_generation

        def change_state_before_commit(
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
                with session._lock:
                    session._workspace_revision = None
            return delivered

        def interrupt_commit_recovery(
            _process: LspProcess,
            _failed_generation: str,
            *,
            deadline: float,
        ) -> None:
            assert time.monotonic() < deadline
            raise SystemExit(41)

        monkeypatch.setattr(LspProcess, "notify_generation", change_state_before_commit)
        monkeypatch.setattr(
            session,
            "_recover_synchronize_snapshot",
            interrupt_commit_recovery,
        )

        with pytest.raises(SystemExit) as raised:
            session.synchronize(revision, deadline=time.monotonic() + 10)

        assert raised.value.code == 41
        traceback_names: list[str] = []
        current = raised.value.__traceback__
        while current is not None:
            traceback_names.append(current.tb_frame.f_code.co_name)
            current = current.tb_next
        assert "interrupt_commit_recovery" in traceback_names
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_synchronize_commit_recovery_unwraps_interruption_without_cycle(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + 10)
        scope = resolve_repository_scope(repository)
        prior = compute_workspace_revision(scope, deadline=time.monotonic() + 10)
        session.synchronize(prior, deadline=time.monotonic() + 10)
        (repository / "pkg/service.py").write_bytes(b"changed = True\n")
        revision = compute_workspace_revision(scope, deadline=time.monotonic() + 10)
        process = session._process
        assert process is not None
        notify_generation = LspProcess.notify_generation
        recovery_interruption = KeyboardInterrupt("wrapped commit recovery interruption")
        recovery_wrapper = RuntimeError("commit recovery interruption wrapper")
        recovery_wrapper.__cause__ = recovery_interruption

        def change_state_before_commit(
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
                with session._lock:
                    session._workspace_revision = None
            return delivered

        def wrap_commit_recovery_interruption(
            _process: LspProcess,
            _failed_generation: str,
            *,
            deadline: float,
        ) -> None:
            assert time.monotonic() < deadline
            raise recovery_wrapper

        monkeypatch.setattr(LspProcess, "notify_generation", change_state_before_commit)
        monkeypatch.setattr(
            session,
            "_recover_synchronize_snapshot",
            wrap_commit_recovery_interruption,
        )

        with pytest.raises(
            KeyboardInterrupt,
            match="wrapped commit recovery interruption",
        ) as raised:
            session.synchronize(revision, deadline=time.monotonic() + 10)

        assert raised.value is recovery_interruption
        assert isinstance(raised.value.__cause__, RuntimeError)
        assert "state changed before commit" in str(raised.value.__cause__)
        assert raised.value.__context__ is None
        assert recovery_wrapper.__cause__ is recovery_interruption
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_synchronize_rejects_noop_recovery_as_unproven(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        document = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        scope = resolve_repository_scope(repository)
        prior = compute_workspace_revision(scope, deadline=time.monotonic() + SHORT_TIMEOUT)
        session.synchronize(prior, deadline=time.monotonic() + SHORT_TIMEOUT)
        process = session._process
        generation = session._generation_nonce
        assert process is not None and generation is not None
        (repository / "pkg/service.py").write_bytes(b"changed = True\n")
        revision = compute_workspace_revision(scope, deadline=time.monotonic() + SHORT_TIMEOUT)
        notify_generation = LspProcess.notify_generation

        def partial_change(
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
                return False
            return delivered

        monkeypatch.setattr(LspProcess, "notify_generation", partial_change)
        monkeypatch.setattr(
            LspProcess,
            "restart",
            lambda _self, _deadline: None,
        )

        with pytest.raises(RuntimeError, match="notification recovery failed"):
            session.synchronize(revision, deadline=time.monotonic() + SHORT_TIMEOUT)

        assert session._workspace_revision is prior
        assert session._documents[document.source.uri] is document
        assert session._generation_nonce is None
        assert session._process is None
        assert session._startup_process is process
        assert session.readiness == "not_ready"
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_unchanged_content_sends_no_did_change_and_keeps_version(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        document = session.open_document(
            "pkg/service.py", deadline=time.monotonic() + 10
        )
        scope = resolve_repository_scope(repository)
        session.synchronize(
            compute_workspace_revision(scope, deadline=time.monotonic() + 10),
            deadline=time.monotonic() + 10,
        )
        delta = session.synchronize(
            compute_workspace_revision(scope, deadline=time.monotonic() + 10),
            deadline=time.monotonic() + 10,
        )
        assert delta.changed == ()
        assert _client_messages(semantic_pyright, "textDocument/didChange") == ()
        assert session._documents[document.source.uri].version == 1
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_created_and_deleted_files_produce_watched_file_events(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + SHORT_TIMEOUT)
        scope = resolve_repository_scope(repository)
        session.synchronize(
            compute_workspace_revision(scope, deadline=time.monotonic() + SHORT_TIMEOUT),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        (repository / "pkg/created.py").write_bytes(b"value = 1\n")
        (repository / "pkg/base.py").unlink()
        session.synchronize(
            compute_workspace_revision(scope, deadline=time.monotonic() + SHORT_TIMEOUT),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        watched = _wait_client_messages(
            semantic_pyright, "workspace/didChangeWatchedFiles"
        )
        assert len(watched) == 1
        types = sorted(
            change["type"] for change in watched[0]["params"]["changes"]
        )
        assert types == [1, 3]
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_watched_file_notification_stays_on_captured_generation(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + SHORT_TIMEOUT)
        scope = resolve_repository_scope(repository)
        prior = compute_workspace_revision(scope, deadline=time.monotonic() + SHORT_TIMEOUT)
        session.synchronize(prior, deadline=time.monotonic() + SHORT_TIMEOUT)
        process = session._process
        generation = session._generation_nonce
        assert process is not None and generation is not None
        (repository / "pkg/watched.py").write_bytes(b"watched = True\n")
        revision = compute_workspace_revision(scope, deadline=time.monotonic() + SHORT_TIMEOUT)
        notify = LspProcess.notify
        notify_generation = LspProcess.notify_generation
        replaced = False

        def replace_generation(deadline: float) -> None:
            nonlocal replaced
            if replaced:
                return
            replaced = True
            process.restart(deadline)
            assert process.generation_nonce != generation

        def replace_before_notify(
            current: LspProcess,
            method: str,
            params: object,
            *,
            deadline: float,
        ) -> None:
            if current is process and method == "workspace/didChangeWatchedFiles":
                replace_generation(deadline)
            notify(current, method, params, deadline=deadline)

        def replace_before_generation_notify(
            current: LspProcess,
            method: str,
            params: object,
            *,
            generation_nonce: str,
            deadline: float,
        ) -> bool:
            if current is process and method == "workspace/didChangeWatchedFiles":
                assert generation_nonce == generation
                replace_generation(deadline)
            return notify_generation(
                current,
                method,
                params,
                generation_nonce=generation_nonce,
                deadline=deadline,
            )

        monkeypatch.setattr(LspProcess, "notify", replace_before_notify)
        monkeypatch.setattr(
            LspProcess,
            "notify_generation",
            replace_before_generation_notify,
        )

        with pytest.raises(RuntimeError, match="watched-files"):
            session.synchronize(revision, deadline=time.monotonic() + SHORT_TIMEOUT)

        assert replaced is True
        assert process.restart_count == 1
        assert process.generation_nonce != generation
        assert _client_messages(
            semantic_pyright,
            "workspace/didChangeWatchedFiles",
        ) == ()
        assert session._workspace_revision is prior
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_deleted_open_document_sends_did_close_and_clears_retention(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        document = session.open_document(
            "pkg/service.py", deadline=time.monotonic() + SHORT_TIMEOUT
        )
        scope = resolve_repository_scope(repository)
        session.synchronize(
            compute_workspace_revision(scope, deadline=time.monotonic() + SHORT_TIMEOUT),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        (repository / "pkg/service.py").unlink()
        session.synchronize(
            compute_workspace_revision(scope, deadline=time.monotonic() + SHORT_TIMEOUT),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        closed = _wait_client_messages(semantic_pyright, "textDocument/didClose")
        assert len(closed) == 1
        assert closed[0]["params"]["textDocument"]["uri"] == document.source.uri
        assert document.source.uri not in session._documents
        assert document.source.uri not in session._diagnostics
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_rename_closes_old_open_uri_and_reports_watched_events(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        document = session.open_document(
            "pkg/rename_target.py", deadline=time.monotonic() + SHORT_TIMEOUT
        )
        scope = resolve_repository_scope(repository)
        session.synchronize(
            compute_workspace_revision(scope, deadline=time.monotonic() + SHORT_TIMEOUT),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        (repository / "pkg/rename_target.py").rename(
            repository / "pkg/renamed.py"
        )
        delta = session.synchronize(
            compute_workspace_revision(scope, deadline=time.monotonic() + SHORT_TIMEOUT),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert delta.renamed == (("pkg/rename_target.py", "pkg/renamed.py"),)
        closed = _wait_client_messages(semantic_pyright, "textDocument/didClose")
        assert len(closed) == 1
        assert closed[0]["params"]["textDocument"]["uri"] == document.source.uri
        watched = _wait_client_messages(
            semantic_pyright, "workspace/didChangeWatchedFiles"
        )
        assert len(watched) == 1
        uris = {
            (change["uri"].rsplit("/", 1)[-1], change["type"])
            for change in watched[0]["params"]["changes"]
        }
        assert ("renamed.py", 1) in uris
        assert ("rename_target.py", 3) in uris
        assert document.source.uri not in session._documents
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


@pytest.mark.parametrize("operation", ["delete", "rename"])
def test_synchronize_removing_readiness_target_revokes_query_ready(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    operation: str,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        document = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert session.readiness == "query_ready"
        scope = resolve_repository_scope(repository)
        session.synchronize(
            compute_workspace_revision(scope, deadline=time.monotonic() + SHORT_TIMEOUT),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        if operation == "delete":
            (repository / "pkg/service.py").unlink()
        else:
            (repository / "pkg/service.py").rename(repository / "pkg/moved.py")

        session.synchronize(
            compute_workspace_revision(scope, deadline=time.monotonic() + SHORT_TIMEOUT),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )

        assert document.source.uri not in session._documents
        assert session._readiness_target_uri is None
        assert session.readiness == "protocol_initialized"
        assert session.workspace_symbols(
            "service",
            deadline=time.monotonic() + SHORT_TIMEOUT,
        ).coverage == "not_ready"
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_content_hash_mismatch_after_revision_fails_stale(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        document = session.open_document(
            "pkg/service.py", deadline=time.monotonic() + SHORT_TIMEOUT
        )
        scope = resolve_repository_scope(repository)
        session.synchronize(
            compute_workspace_revision(scope, deadline=time.monotonic() + SHORT_TIMEOUT),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        (repository / "pkg/service.py").write_bytes(
            b"class First:\n    pass\n"
        )
        revision = compute_workspace_revision(
            scope, deadline=time.monotonic() + SHORT_TIMEOUT
        )
        (repository / "pkg/service.py").write_bytes(
            b"class Second:\n    pass\n"
        )
        with pytest.raises(RuntimeError, match="hash differs from the revision"):
            session.synchronize(revision, deadline=time.monotonic() + SHORT_TIMEOUT)
        assert _client_messages(semantic_pyright, "textDocument/didChange") == ()
        assert session._documents[document.source.uri].version == 1
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_synchronize_rejects_foreign_checkout_revision(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    tmp_path: Path,
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + SHORT_TIMEOUT)
        foreign = replace(
            compute_workspace_revision(
                resolve_repository_scope(repository),
                deadline=time.monotonic() + SHORT_TIMEOUT,
            ),
            checkout_id="deadbeef",
        )
        with pytest.raises(ValueError, match="must describe this checkout"):
            session.synchronize(foreign, deadline=time.monotonic() + SHORT_TIMEOUT)
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_synchronize_does_not_persist_semantic_results(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + SHORT_TIMEOUT)
        scope = resolve_repository_scope(repository)
        session.synchronize(
            compute_workspace_revision(scope, deadline=time.monotonic() + SHORT_TIMEOUT),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        cache_root = state_root / "cache"
        semantic_files = (
            list(cache_root.rglob("*.json")) if cache_root.exists() else []
        )
        lsp_owners = list((state_root / "run" / "lsp").iterdir()) if (
            state_root / "run" / "lsp"
        ).exists() else []
        assert semantic_files == []
        for owner in lsp_owners:
            assert not (owner / "diagnostics.json").exists()
            assert not (owner / "results.json").exists()
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)
