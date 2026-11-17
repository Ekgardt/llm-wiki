"""Open documents of the pinned Pyright session: readiness probes, source reads, locks and bounds."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

import pytest
from llm_wiki import (
    pyright_session_documents,
)
from llm_wiki.pyright_session_core import PyrightSession
from llm_wiki.pyright_session_values import (
    OpenDocument,
    ProviderLocations,
)
from lsp_process import LspProcess
from lsp_protocol import MAX_FRAME_BYTES
from lsp_security import RepositorySource
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
    CONTENTION_HOLD_SECONDS,
    CONTENTION_RETURN_SECONDS,
    _anchor,
    _client_events,
    _client_methods,
    _client_pids,
    _join_all,
    _session,
    semantic_pyright,  # noqa: F401
)
from tests.slow_machine import SHORT_TIMEOUT


def _probe_uris(messages, pid: int) -> list[str]:
    return [
        event["message"]["params"]["textDocument"]["uri"]
        for event in messages
        if event["pid"] == pid and event["method"] == "textDocument/documentSymbol"
    ]


def _method_events(events, pid: int, method: str) -> int:
    return sum(
        1
        for event in events
        if event["pid"] == pid and event.get("method") == method
    )


def test_open_document_establishes_exact_query_readiness_and_reopens_once(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    scope = resolve_repository_scope(repository)
    content = (repository / "pkg/service.py").read_bytes()
    try:
        document = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert document == OpenDocument(
            RepositorySource(
                scope.repository_id,
                scope.checkout_id,
                "pkg/service.py",
                (repository / "pkg/service.py").resolve(),
                (repository / "pkg/service.py").resolve().as_uri(),
            ),
            content,
            hashlib.sha256(content).hexdigest(),
            1,
        )
        assert session.readiness == "query_ready"
        assert session.readiness_evidence == (
            "initialize",
            "initialized",
            "configuration",
            "didOpen",
            "documentSymbol",
        )

        reopened = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert reopened is document
        messages = [
            event["message"]
            for event in semantic_pyright.events()
            if event["kind"] == "client-message"
        ]
        assert [message["method"] for message in messages] == [
            "initialize",
            "initialized",
            "workspace/didChangeConfiguration",
            "textDocument/didOpen",
            "textDocument/documentSymbol",
        ]
        assert messages[3]["params"] == {
            "textDocument": {
                "uri": document.source.uri,
                "languageId": "python",
                "version": 1,
                "text": content.decode("utf-8"),
            }
        }
        assert messages[4]["params"] == {
            "textDocument": {"uri": document.source.uri}
        }
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_concurrent_first_open_sends_one_didopen(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    open_document_type = OpenDocument
    construction_lock = threading.Lock()
    second_construction = threading.Event()
    start = threading.Barrier(3)
    construction_count = 0
    documents: list[OpenDocument] = []
    errors: list[BaseException] = []

    def delayed_construction(*args: object) -> OpenDocument:
        nonlocal construction_count
        with construction_lock:
            construction_count += 1
            current = construction_count
        if current == 1:
            second_construction.wait(0.5)
        else:
            second_construction.set()
        return open_document_type(*args)  # type: ignore[arg-type]

    def open_source() -> None:
        start.wait()
        try:
            documents.append(
                session.open_document(
                    "pkg/service.py",
                    deadline=time.monotonic() + SHORT_TIMEOUT,
                )
            )
        except BaseException as error:
            errors.append(error)

    session.start(deadline=time.monotonic() + SHORT_TIMEOUT)
    monkeypatch.setattr(pyright_session_documents, "OpenDocument", delayed_construction)
    threads = (threading.Thread(target=open_source), threading.Thread(target=open_source))
    for thread in threads:
        thread.start()
    start.wait()
    try:
        _join_all(threads)
        assert errors == []
        assert len(documents) == 2
        assert documents[0] is documents[1]
        assert _client_methods(semantic_pyright).count("textDocument/didOpen") == 1
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_open_racing_restart_sends_didopen_once_for_replacement_generation(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    real_notify = LspProcess.notify
    real_notify_generation = LspProcess.notify_generation
    restarted = False

    def restart_once(process: LspProcess, method: str, deadline: float) -> None:
        nonlocal restarted
        if method == "textDocument/didOpen" and not restarted:
            restarted = True
            process.restart(deadline)

    def notify(
        process: LspProcess,
        method: str,
        params: object,
        *,
        deadline: float,
    ) -> None:
        restart_once(process, method, deadline)
        real_notify(process, method, params, deadline=deadline)

    def notify_generation(
        process: LspProcess,
        method: str,
        params: object,
        *,
        generation_nonce: str,
        deadline: float,
    ) -> bool:
        restart_once(process, method, deadline)
        return real_notify_generation(
            process,
            method,
            params,
            generation_nonce=generation_nonce,
            deadline=deadline,
        )

    try:
        session.start(deadline=time.monotonic() + SHORT_TIMEOUT)
        monkeypatch.setattr(LspProcess, "notify", notify)
        monkeypatch.setattr(LspProcess, "notify_generation", notify_generation)

        session.open_document("pkg/service.py", deadline=time.monotonic() + SHORT_TIMEOUT)

        process = session._process
        assert restarted is True
        assert process is not None
        assert process.restart_count == 1
        events = semantic_pyright.events()
        pids = _client_pids(events)
        assert len(pids) == 2
        assert _method_events(events, pids[-1], "textDocument/didOpen") == 1
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_failed_generation_notify_records_no_didopen_and_retries_later(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    real_notify_generation = LspProcess.notify_generation

    def reject_notify(
        _process: LspProcess,
        _method: str,
        _params: object,
        *,
        generation_nonce: str,
        deadline: float,
    ) -> bool:
        assert generation_nonce
        assert deadline > time.monotonic()
        return False

    try:
        session.start(deadline=time.monotonic() + 10)
        monkeypatch.setattr(LspProcess, "notify_generation", reject_notify)
        first = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        )
        assert not any(
            event.get("method") == "textDocument/didOpen"
            for event in semantic_pyright.events()
        )
        assert "didOpen" not in session.readiness_evidence

        monkeypatch.setattr(LspProcess, "notify_generation", real_notify_generation)
        second = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        )
        assert second is first
        assert sum(
            event.get("method") == "textDocument/didOpen"
            for event in semantic_pyright.events()
        ) == 1
    finally:
        session.close(deadline=time.monotonic() + 5)


def test_failed_readiness_probe_retries_without_duplicate_didopen(
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"document_symbol_failures": 1},
    )
    session = _session(repository, state_root, fixture)
    try:
        first = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert session.readiness == "protocol_initialized"
        assert session.readiness_evidence == (
            "initialize",
            "initialized",
            "configuration",
            "didOpen",
        )
        second = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert second is first
        assert session.readiness == "query_ready"
        methods = [
            event["method"]
            for event in fixture.events()
            if event["kind"] == "client-message"
        ]
        assert methods.count("textDocument/didOpen") == 1
        assert methods.count("textDocument/documentSymbol") == 2
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_ready_second_document_cannot_authorize_failed_first_document(
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"document_symbol_failure_uris": ["$SERVICE_URI"]},
    )
    session = _session(repository, state_root, fixture)
    try:
        session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        session.open_document(
            "pkg/api.py",
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert session.readiness == "query_ready"

        result = session.definition(
            _anchor(repository, "pkg/service.py", 10, 20),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )

        assert result == ProviderLocations((), "not_ready", True)
        assert "textDocument/definition" not in _client_methods(fixture)
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_restart_rebuilds_readiness_per_replayed_document(
    repository: Path,
    state_root: Path,
) -> None:
    marker = repository / ".fake-lsp-per-document-crash"
    fixture = create_semantic_pyright_fixture(
        repository,
        config={
            "crash_once_method": "textDocument/definition",
            "crash_marker": str(marker),
        },
    )
    session = _session(repository, state_root, fixture)
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + SHORT_TIMEOUT)
        session.open_document("pkg/api.py", deadline=time.monotonic() + SHORT_TIMEOUT)
        config = json.loads(fixture.config_path.read_text(encoding="utf-8"))
        config["document_symbol_failure_uris"] = ["$SERVICE_URI"]
        fixture.config_path.write_text(
            json.dumps(config, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )

        result = session.definition(
            _anchor(repository, "pkg/service.py", 10, 20),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )

        assert result == ProviderLocations((), "not_ready", True)
        assert session.readiness == "query_ready"
        messages = _client_events(fixture.events())
        pids = _client_pids(messages)
        assert len(pids) == 2
        assert _probe_uris(messages, pids[1]) == [
            (repository / "pkg/service.py").resolve().as_uri(),
            (repository / "pkg/api.py").resolve().as_uri(),
        ]
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_missing_document_symbol_capability_never_sends_probe_or_claims_ready(
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"capabilities": {"definitionProvider": True}},
    )
    session = _session(repository, state_root, fixture)
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + SHORT_TIMEOUT)
        assert session.readiness == "protocol_initialized"
        assert session.readiness_evidence == (
            "initialize",
            "initialized",
            "configuration",
            "didOpen",
        )
        methods = [
            event["method"]
            for event in fixture.events()
            if event["kind"] == "client-message"
        ]
        assert "textDocument/documentSymbol" not in methods
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_malformed_document_symbol_list_cannot_establish_readiness(
    repository: Path,
    state_root: Path,
) -> None:
    fixture = create_semantic_pyright_fixture(
        repository,
        config={"responses": {"textDocument/documentSymbol": [42]}},
    )
    session = _session(repository, state_root, fixture)
    try:
        session.open_document("pkg/service.py", deadline=time.monotonic() + SHORT_TIMEOUT)
        assert session.readiness == "protocol_initialized"
        assert session.readiness_evidence == (
            "initialize",
            "initialized",
            "configuration",
            "didOpen",
        )

        result = session.definition(
            _anchor(repository, "pkg/service.py", 10, 20),
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert result == ProviderLocations((), "not_ready", True)
        assert "textDocument/definition" not in _client_methods(fixture)
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_document_symbol_normalizer_is_strict_and_accepts_empty_list(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    uri = (repository / "pkg/service.py").resolve().as_uri()
    selection = {
        "start": {"line": 0, "character": 0},
        "end": {"line": 0, "character": 1},
    }

    assert session._normalize_document_symbols([], uri) == ((), False)
    assert session._normalize_document_symbols(
        [{"selectionRange": selection}],
        uri,
    ) == ((), True)


@pytest.mark.parametrize(
    ("name", "content_kind", "error"),
    [
        ("invalid.py", "invalid", UnicodeDecodeError),
        ("oversized.py", "oversized", ValueError),
    ],
)
def test_open_document_rejects_invalid_utf8_and_frame_sized_content(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
    name: str,
    content_kind: str,
    error: type[Exception],
) -> None:
    content = (
        b"value = '\xff'\n"
        if content_kind == "invalid"
        else b"x" * MAX_FRAME_BYTES
    )
    (repository / name).write_bytes(content)
    session = _session(repository, state_root, semantic_pyright)
    try:
        with pytest.raises(error):
            session.open_document(name, deadline=time.monotonic() + SHORT_TIMEOUT)
        methods = [
            event["method"]
            for event in semantic_pyright.events()
            if event["kind"] == "client-message"
        ]
        assert "textDocument/didOpen" not in methods
        assert session.readiness == "protocol_initialized"
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_open_document_propagates_deadline_to_stable_source_read(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    session.start(deadline=time.monotonic() + 10)
    deadline = time.monotonic() + 5
    observed: list[float] = []

    def expiring_read(
        _path: Path,
        _max_bytes: int,
        *,
        label: str,
        deadline: float,
    ) -> bytes:
        assert label == "Pyright source document"
        observed.append(deadline)
        raise TimeoutError("stable read deadline expired")

    monkeypatch.setattr(pyright_session_documents, "read_stable_bytes", expiring_read)
    try:
        with pytest.raises(TimeoutError, match="deadline"):
            session.open_document("pkg/service.py", deadline=deadline)
        assert observed == [deadline]
        methods = [
            event["method"]
            for event in semantic_pyright.events()
            if event["kind"] == "client-message"
        ]
        assert "textDocument/didOpen" not in methods
    finally:
        session.close(deadline=time.monotonic() + 5)


def _hold_document_lock_for_contention(
    session: PyrightSession,
    held: threading.Event,
    released: threading.Event,
    release_request: threading.Event,
) -> None:
    with session._document_lock:
        held.set()
        release_request.wait(CONTENTION_HOLD_SECONDS)
    released.set()


def test_open_document_lock_contention_obeys_deadline_without_mutation(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    held = threading.Event()
    released = threading.Event()
    release_request = threading.Event()
    holder = threading.Thread(
        target=_hold_document_lock_for_contention,
        args=(session, held, released, release_request),
    )
    holder.start()
    assert held.wait(SHORT_TIMEOUT)
    before_state = (
        session._readiness,
        session._readiness_evidence,
        session._degradation_codes,
        session._active_operations,
        session._last_used_monotonic,
        session._process,
        session._startup_attempted,
        session._starting,
        dict(session._documents),
        session._document_bytes,
        session._workspace_revision,
        session._synchronize_epoch,
        session._generation_nonce,
    )
    before_events = tuple(semantic_pyright.events())
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="document lock"):
            session.open_document(
                "pkg/service.py",
                deadline=started + 0.05,
            )
        assert time.monotonic() - started < CONTENTION_RETURN_SECONDS
        assert released.is_set() is False
        assert (
            session._readiness,
            session._readiness_evidence,
            session._degradation_codes,
            session._active_operations,
            session._last_used_monotonic,
            session._process,
            session._startup_attempted,
            session._starting,
            dict(session._documents),
            session._document_bytes,
            session._workspace_revision,
            session._synchronize_epoch,
            session._generation_nonce,
        ) == before_state
        assert tuple(semantic_pyright.events()) == before_events
        assert not (state_root / "run/lsp").exists()
    finally:
        release_request.set()
        holder.join(SHORT_TIMEOUT)
        session.close(deadline=time.monotonic() + 30)
    assert not holder.is_alive()


def test_synchronize_lock_contention_obeys_deadline_without_mutation(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    revision = compute_workspace_revision(
        resolve_repository_scope(repository),
        deadline=time.monotonic() + 5,
    )
    held = threading.Event()
    released = threading.Event()
    release_request = threading.Event()
    holder = threading.Thread(
        target=_hold_document_lock_for_contention,
        args=(session, held, released, release_request),
    )
    holder.start()
    assert held.wait(SHORT_TIMEOUT)
    before_state = (
        session._readiness,
        session._readiness_evidence,
        session._degradation_codes,
        session._active_operations,
        session._last_used_monotonic,
        session._process,
        session._startup_attempted,
        session._starting,
        dict(session._documents),
        session._document_bytes,
        session._workspace_revision,
        session._synchronize_epoch,
        session._generation_nonce,
    )
    before_events = tuple(semantic_pyright.events())
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="document lock"):
            session.synchronize(revision, deadline=started + 0.05)
        assert time.monotonic() - started < CONTENTION_RETURN_SECONDS
        assert released.is_set() is False
        assert (
            session._readiness,
            session._readiness_evidence,
            session._degradation_codes,
            session._active_operations,
            session._last_used_monotonic,
            session._process,
            session._startup_attempted,
            session._starting,
            dict(session._documents),
            session._document_bytes,
            session._workspace_revision,
            session._synchronize_epoch,
            session._generation_nonce,
        ) == before_state
        assert tuple(semantic_pyright.events()) == before_events
        assert not (state_root / "run/lsp").exists()
    finally:
        release_request.set()
        holder.join(SHORT_TIMEOUT)
        session.close(deadline=time.monotonic() + 30)
    assert not holder.is_alive()


def test_document_lock_post_acquire_interruption_releases_and_remains_usable(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    session = _session(repository, state_root, semantic_pyright)
    underlying = session._document_lock

    class InterruptAfterAcquire:
        def __init__(self) -> None:
            self.interrupt = True

        def acquire(self, *, timeout: float) -> bool:
            acquired = underlying.acquire(timeout=timeout)
            if acquired and self.interrupt:
                self.interrupt = False
                raise KeyboardInterrupt("document lock interrupted after acquire")
            return acquired

        def release(self) -> None:
            underlying.release()

        def cleanup_failed_test(self) -> None:
            try:
                underlying.release()
            except RuntimeError:
                pass

    interrupting_lock = InterruptAfterAcquire()
    session._document_lock = interrupting_lock  # type: ignore[assignment]
    try:
        with pytest.raises(
            KeyboardInterrupt,
            match="document lock interrupted after acquire",
        ):
            session.open_document(
                "pkg/service.py",
                deadline=time.monotonic() + 5,
            )

        available = underlying.acquire(blocking=False)
        if available:
            underlying.release()
        assert available

        document = session.open_document(
            "pkg/service.py",
            deadline=time.monotonic() + 10,
        )
        revision = compute_workspace_revision(
            resolve_repository_scope(repository),
            deadline=time.monotonic() + 10,
        )
        assert session.synchronize(
            revision,
            deadline=time.monotonic() + 10,
        ) == WorkspaceDelta((), (), (), ())
        assert session._documents[document.source.uri] is document
    finally:
        interrupting_lock.cleanup_failed_test()
        session.close(deadline=time.monotonic() + 5)


def test_multimegabyte_document_below_frame_limit_can_be_opened(
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    content = b"# " + b"x" * (2 * 1024 * 1024) + b"\n"
    (repository / "large.py").write_bytes(content)
    session = _session(repository, state_root, semantic_pyright)
    try:
        document = session.open_document(
            "large.py",
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
        assert document.content == content
        assert session.readiness == "query_ready"
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_document_count_limit_closes_the_oldest_without_partial_session_state(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    # Before 2026-09-17 the document past the count limit raised and the
    # precise tier was over for every new file; it now closes the one
    # untouched longest and takes its place. See finding K-A12.
    first_path = repository / "first.py"
    second_path = repository / "second.py"
    first_path.write_text("first = 1\n", encoding="utf-8")
    second_path.write_text("second = 2\n", encoding="utf-8")
    monkeypatch.setattr(
        pyright_session_documents,
        "MAX_OPEN_DOCUMENTS",
        1,
        raising=False,
    )
    session = _session(repository, state_root, semantic_pyright)
    try:
        session.open_document("first.py", deadline=time.monotonic() + SHORT_TIMEOUT)
        second = session.open_document("second.py", deadline=time.monotonic() + SHORT_TIMEOUT)
        methods = [event.get("method") for event in semantic_pyright.events()]
        assert (
            session._documents,
            session._readiness_target_uri,
            session._document_bytes,
            (methods.count("textDocument/didOpen"), methods.count("textDocument/didClose")),
        ) == (
            {second.source.uri: second},
            second.source.uri,
            len(second.content),
            (2, 1),
        )
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)


def test_document_byte_limit_rejects_without_partial_session_state(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    state_root: Path,
    semantic_pyright: SemanticPyrightFixture,  # noqa: F811
) -> None:
    first_path = repository / "first.py"
    second_path = repository / "second.py"
    first_path.write_text("first = 1\n", encoding="utf-8")
    second_path.write_text("second = 2\n", encoding="utf-8")
    first_size = first_path.stat().st_size
    monkeypatch.setattr(
        pyright_session_documents,
        "MAX_OPEN_DOCUMENT_BYTES",
        first_size,
        raising=False,
    )
    session = _session(repository, state_root, semantic_pyright)
    try:
        first = session.open_document("first.py", deadline=time.monotonic() + SHORT_TIMEOUT)
        target = session._readiness_target_uri
        with pytest.raises(RuntimeError, match="document source bytes"):
            session.open_document("second.py", deadline=time.monotonic() + SHORT_TIMEOUT)

        assert session._documents == {first.source.uri: first}
        assert session._readiness_target_uri == target
        assert session._document_bytes == len(first.content)
    finally:
        session.close(deadline=time.monotonic() + SHORT_TIMEOUT)
