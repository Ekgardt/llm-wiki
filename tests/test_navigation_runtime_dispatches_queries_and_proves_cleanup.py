from __future__ import annotations

import importlib
import importlib.util
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from code_intelligence import Capability
from code_navigation import NavigationRequest
from repository_scope import resolve_repository_scope

from tests.retrieval_v2_helpers import BENCHMARK
from tests.slow_machine import SHORT_TIMEOUT

ownership = importlib.import_module("navigation_benchmark_ownership")
SCRIPTS = BENCHMARK.parent / "scripts"


class FakePopen:
    def __init__(self, returncode: int | None = 0) -> None:
        self.returncode = returncode
        self.killed = 0

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.killed += 1


class FakeSession:
    def __init__(self, scope: object, identity: object, *, state_root: Path) -> None:
        self.opened_with = (scope, identity, state_root)
        self.calls: list[tuple[str, object]] = []
        self.position_encoding = "utf-16"
        self._process: object = None

    def synchronize(self, revision: object, *, deadline: float) -> None:
        self.calls.append(("synchronize", revision))

    def definition(self, anchor: object, *, deadline: float) -> str:
        self.calls.append(("definition", anchor))
        return "definition"

    def references(self, anchor: object, *, deadline: float) -> str:
        self.calls.append(("references", anchor))
        return "references"

    def incoming_calls(self, anchor: object, *, deadline: float) -> str:
        self.calls.append(("incoming", anchor))
        return "incoming"

    def outgoing_calls(self, anchor: object, *, deadline: float) -> str:
        self.calls.append(("outgoing", anchor))
        return "outgoing"


class FakeNavigation:
    def __init__(self, scope: object, session: FakeSession, identity: object) -> None:
        self.session = session
        self.queries: list[object] = []
        self.closed = 0

    def query(self, request: object, *, deadline: float) -> str:
        self.queries.append(request)
        return f"answer-{len(self.queries)}"

    def close(self, *, deadline: float) -> None:
        self.closed += 1


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(ownership, "PyrightSession", FakeSession)
    monkeypatch.setattr(ownership, "CodeNavigation", FakeNavigation)
    monkeypatch.setattr(ownership, "compute_workspace_revision", lambda scope, deadline: "rev-1")
    (tmp_path / "module.py").write_text("def target():\n    return 1\n", encoding="utf-8")
    scope = resolve_repository_scope(tmp_path)
    repository = SimpleNamespace(root=tmp_path)
    return ownership._RealNavigationRuntime(repository, scope, "identity", tmp_path / "state")


def _request(runtime: Any, capability: Capability, direction: str | None = None) -> Any:
    return NavigationRequest(
        repository=runtime.scope,
        capability=capability,
        path="module.py",
        line=1,
        character=4,
        direction=direction,
    )


def _live_process(owner_root: Path) -> SimpleNamespace:
    return SimpleNamespace(process=FakePopen(), _coordinator=None, owner_root=owner_root)


def test_a_new_runtime_opens_a_session_and_a_navigation_over_it(runtime: Any) -> None:
    session = runtime._session

    assert session.opened_with == (runtime.scope, "identity", runtime.state_root)
    assert runtime._navigation.session is session
    assert runtime.position_encoding == "utf-16"


def test_a_query_is_remembered_for_the_ownership_probe(runtime: Any) -> None:
    request = _request(runtime, Capability.DEFINITIONS)

    assert runtime.query(request, deadline=time.monotonic() + SHORT_TIMEOUT) == "answer-1"
    assert runtime._last_request is request


def test_synchronize_hands_the_workspace_revision_to_the_session(runtime: Any) -> None:
    runtime.synchronize(deadline=time.monotonic() + SHORT_TIMEOUT)

    assert runtime._session.calls == [("synchronize", "rev-1")]


@pytest.mark.parametrize(
    ("capability", "direction", "answer"),
    [
        (Capability.DEFINITIONS, None, "definition"),
        (Capability.REFERENCES, None, "references"),
        (Capability.CALLS, "incoming", "incoming"),
        (Capability.CALLS, "outgoing", "outgoing"),
    ],
)
def test_a_direct_query_synchronizes_then_asks_the_session_for_its_capability(
    runtime: Any, capability: Capability, direction: str | None, answer: str
) -> None:
    result = runtime.direct_query(
        _request(runtime, capability, direction), deadline=time.monotonic() + SHORT_TIMEOUT
    )

    calls = runtime._session.calls
    assert result == answer
    assert [name for name, _value in calls] == ["synchronize", answer]
    assert calls[1][1].line == 1


def test_a_direct_query_for_an_unqualified_capability_is_refused(runtime: Any) -> None:
    with pytest.raises(ValueError, match="unsupported direct qualification capability"):
        runtime.direct_query(
            _request(runtime, Capability.TYPES), deadline=time.monotonic() + SHORT_TIMEOUT
        )


def test_crash_and_recover_kills_the_server_answers_again_and_reopens(
    runtime: Any, tmp_path: Path
) -> None:
    crashed_session = runtime._session
    crashed_navigation = runtime._navigation
    process = _live_process(tmp_path)
    crashed_session._process = process

    recovered = runtime.crash_and_recover(
        _request(runtime, Capability.DEFINITIONS), deadline=time.monotonic() + SHORT_TIMEOUT
    )

    assert recovered == "answer-2"
    assert process.process.killed == 1
    assert crashed_navigation.closed == 1
    assert runtime._session is not crashed_session


def test_crash_and_recover_without_a_live_process_measures_nothing(runtime: Any) -> None:
    navigation = runtime._navigation

    assert (
        runtime.crash_and_recover(
            _request(runtime, Capability.DEFINITIONS), deadline=time.monotonic() + SHORT_TIMEOUT
        )
        is None
    )
    assert navigation.queries != []
    assert navigation.closed == 0


def test_a_failed_cleanup_is_terminal_for_every_later_reset(runtime: Any) -> None:
    runtime._cleanup_failed = True

    with pytest.raises(ownership._CleanupProofError, match="runtime cleanup is terminal"):
        runtime._reset(time.monotonic() + SHORT_TIMEOUT)


@pytest.mark.parametrize(
    ("returncode", "lease_present", "retained"), [(0, False, 0), (None, False, 1), (0, True, 1)]
)
def test_close_counts_a_live_process_or_a_remaining_lease_as_retained(
    runtime: Any, tmp_path: Path, returncode: int | None, lease_present: bool, retained: int
) -> None:
    process = _live_process(tmp_path)
    process.process = FakePopen(returncode)
    runtime._session._process = process
    _write_lease(tmp_path, lease_present)

    assert runtime.close(deadline=time.monotonic() + SHORT_TIMEOUT) == retained
    assert runtime._navigation.closed == 1


def _write_lease(owner_root: Path, present: bool) -> None:
    if present:
        (owner_root / "lease.json").write_text("{}", encoding="utf-8")


def test_without_a_process_nothing_is_retained() -> None:
    assert ownership._process_handles(None) == (None, None, None)
    assert ownership._process_alive(None) is False
    assert ownership._coordinator_owns(None) is False
    assert ownership._lease_exists(None) is False


def test_only_the_timeout_scenario_gets_the_short_probe_budget() -> None:
    assert ownership._probe_request_deadline("cancellation", 10.0, 50.0) == 50.0
    assert ownership._probe_request_deadline("timeout", 10.0, 50.0) == pytest.approx(
        10.0 + ownership._OWNERSHIP_TIMEOUT_SECONDS
    )


def test_a_poll_after_completion_returns_without_waiting_past_the_deadline() -> None:
    completed = threading.Event()
    completed.set()
    started = time.monotonic()

    ownership._wait_one_poll(completed, started)

    assert time.monotonic() - started < 1.0


def _bare_runtime() -> Any:
    bare = object.__new__(ownership._RealNavigationRuntime)
    bare._cleanup_failed = False
    return bare


def test_a_probe_request_that_never_terminates_fails_cleanup() -> None:
    bare = _bare_runtime()
    worker = threading.Thread(target=lambda: None)
    worker.start()
    worker.join()

    with pytest.raises(ownership._CleanupProofError, match="request did not terminate"):
        bare._join_probe_worker(worker, threading.Event(), time.monotonic() - 1)
    assert bare.cleanup_failed is True


def test_a_probe_owner_thread_that_outlives_its_request_fails_cleanup() -> None:
    bare = _bare_runtime()
    release = threading.Event()
    completed = threading.Event()
    completed.set()
    worker = threading.Thread(target=release.wait)
    worker.start()

    try:
        with pytest.raises(ownership._CleanupProofError, match="owner did not stop"):
            bare._join_probe_worker(worker, completed, time.monotonic() - 1)
    finally:
        release.set()
        worker.join()
    assert bare.cleanup_failed is True


def test_no_ownership_attempt_starts_after_cleanup_failed() -> None:
    bare = _bare_runtime()
    bare._cleanup_failed = True

    assert bare._ownership_attempt_or_stop("crash", time.monotonic() + SHORT_TIMEOUT) == (
        None,
        False,
    )


class RefusingProtocol:
    generation_nonce = "nonce"

    def _queue_request_message(self, message: object, *, deadline: float, key: object) -> None:
        raise AssertionError("not queued in this test")

    def _handle_response(self, message: object, nonce: object) -> None:
        raise AssertionError("not answered in this test")

    def __setattr__(self, name: str, value: object) -> None:
        if name == "_handle_response":
            raise AttributeError("read-only reply handler")
        object.__setattr__(self, name, value)


def test_a_reply_scope_that_cannot_install_its_hooks_restores_the_protocol() -> None:
    protocol = RefusingProtocol()

    with (
        pytest.raises(AttributeError, match="read-only reply handler"),
        ownership._ProbeReplyDelivery(protocol),
    ):
        raise AssertionError("the scope must not be entered")

    assert vars(protocol) == {}


def test_loading_the_module_puts_scripts_and_benchmark_on_the_import_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = {str(SCRIPTS), str(BENCHMARK)}
    monkeypatch.setattr(sys, "path", [entry for entry in sys.path if entry not in roots])
    spec = importlib.util.spec_from_file_location(
        "navigation_benchmark_ownership_copy", BENCHMARK / "navigation_benchmark_ownership.py"
    )
    assert spec is not None
    assert spec.loader is not None

    spec.loader.exec_module(importlib.util.module_from_spec(spec))

    assert roots <= set(sys.path)


@pytest.mark.parametrize(
    ("orphan", "problem"),
    [
        (0, None),
        (1, "runtime cleanup reported retained ownership"),
        (True, "runtime cleanup returned invalid evidence"),
        (2, "runtime cleanup returned invalid evidence"),
    ],
)
def test_only_an_integer_zero_proves_cleanup(orphan: object, problem: str | None) -> None:
    assert ownership._orphan_evidence_problem(orphan) == problem


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_an_interpreter_interruption_during_a_probe_is_never_swallowed(
    interruption: type[BaseException],
) -> None:
    with pytest.raises(interruption):
        ownership._check_probe_error(interruption(), "cancellation")


def test_an_ownership_probe_needs_a_prior_request(runtime: Any) -> None:
    with pytest.raises(ownership._OwnershipProbeError) as failure:
        runtime._prepare_process(time.monotonic() + SHORT_TIMEOUT)

    assert failure.value.code == "no_prior_request"


def test_an_ownership_probe_needs_a_live_process(runtime: Any) -> None:
    runtime.query(
        _request(runtime, Capability.DEFINITIONS), deadline=time.monotonic() + SHORT_TIMEOUT
    )

    with pytest.raises(ownership._OwnershipProbeError) as failure:
        runtime._prepare_process(time.monotonic() + SHORT_TIMEOUT)

    assert failure.value.code == "no_live_process"


def test_an_ownership_probe_reissues_the_last_request_on_the_live_process(
    runtime: Any, tmp_path: Path
) -> None:
    request = _request(runtime, Capability.REFERENCES)
    runtime.query(request, deadline=time.monotonic() + SHORT_TIMEOUT)
    runtime._session._process = _live_process(tmp_path)

    prepared = runtime._prepare_process(time.monotonic() + SHORT_TIMEOUT)

    assert prepared == (request, runtime._session._process)
    assert runtime._navigation.queries == [request, request]
