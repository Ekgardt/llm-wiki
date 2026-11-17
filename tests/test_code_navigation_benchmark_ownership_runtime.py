"""Lifecycle-ownership scenarios and crash recovery of the real navigation runtime the benchmark drives."""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

BENCHMARK_ROOT = Path(__file__).resolve().parent.parent / "benchmark"
SCRIPTS_ROOT = Path(__file__).resolve().parent.parent / "scripts"
for path in (BENCHMARK_ROOT, SCRIPTS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import navigation_benchmark_ownership as ownership  # noqa: E402
from code_intelligence import Capability  # noqa: E402
from code_navigation import (  # noqa: E402
    NavigationResult,
    NavigationStatus,
)
from lsp_protocol import RequestCancelled  # noqa: E402
from navigation_benchmark_ownership import _RealNavigationRuntime  # noqa: E402

from tests.code_navigation_benchmark_runtime_helpers import (  # noqa: E402
    _empty_navigation_result,
)
from tests.slow_machine import SHORT_TIMEOUT  # noqa: E402


def _terminal_probe(scenario, sent, terminal, events, deadline, cancellation):
    """The probe is interrupted before dispatch, or after it."""
    if _interruption_pending(deadline, cancellation):
        terminal.set()
        raise _interruption(scenario, "rejected before dispatch")
    sent.set()
    events.append("sent")
    if scenario == "cancellation":
        _await_cancellation(cancellation, deadline)
        terminal.set()
        events.append("terminal")
        raise RequestCancelled("cancelled after dispatch")
    _await_deadline(deadline)
    terminal.set()
    events.append("terminal")
    raise TimeoutError("timed out after dispatch")


class _ProbeDeliveryProtocolShape:
    generation_nonce = "benchmark-fixture-generation"

    def _queue_request_message(self, message, *, deadline, key):
        raise AssertionError("this terminal fixture does not queue wire messages")

    def _handle_response(self, message, nonce):
        raise AssertionError("this terminal fixture does not receive wire replies")


@pytest.mark.parametrize("scenario", ["timeout", "cancellation"])
def test_ownership_interruption_rejects_terminal_without_sent_proof(
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    terminal = threading.Event()

    class Protocol(_ProbeDeliveryProtocolShape):
        @staticmethod
        def _sent_request_evidence() -> tuple[int, str | None]:
            return 0, None

    class Process:
        protocol = Protocol()

        @staticmethod
        def request(*args, **kwargs):
            terminal.set()
            if scenario == "timeout":
                raise TimeoutError("terminal without dispatch")
            raise RequestCancelled("terminal without dispatch")

    class Runtime(_RealNavigationRuntime):
        def __init__(self) -> None:
            self._cleanup_failed = False
            self.reset_calls = 0

        def _prepare_process(self, deadline: float):
            return object(), Process()

        def _reset(self, deadline: float) -> int:
            assert terminal.is_set()
            self.reset_calls += 1
            return 0

    monkeypatch.setattr(ownership, "_OWNERSHIP_SCENARIOS", (scenario,))
    runtime = Runtime()

    outcomes = runtime.ownership_checks(deadline=time.monotonic() + 2.0)

    assert outcomes == {scenario: {"available": False, "orphan_count": None}}
    assert runtime.reset_calls == 1


def _interruption_pending(deadline: float, cancellation) -> bool:
    """The request is over before dispatch: out of time, or already cancelled."""
    if deadline <= time.monotonic():
        return True
    if cancellation is None:
        return False
    return cancellation.is_cancelled()


def _interruption(scenario: str, message: str) -> Exception:
    if scenario == "timeout":
        return TimeoutError(message)
    return RequestCancelled(message)


def _await_cancellation(cancellation, deadline: float) -> None:
    assert cancellation is not None
    assert cancellation.wait(max(0.0, deadline - time.monotonic()))


def _await_deadline(deadline: float) -> None:
    while time.monotonic() < deadline:
        time.sleep(0.001)


@pytest.mark.parametrize("scenario", ["timeout", "cancellation"])
def test_ownership_interruption_dispatches_before_terminal_and_then_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    sent = threading.Event()
    terminal = threading.Event()
    events: list[str] = []

    def probe(deadline: float, cancellation):
        return _terminal_probe(scenario, sent, terminal, events, deadline, cancellation)

    class Protocol(_ProbeDeliveryProtocolShape):
        @staticmethod
        def _sent_request_evidence() -> tuple[int, str | None]:
            return (1, "workspace/symbol") if sent.is_set() else (0, None)

    class Process:
        protocol = Protocol()

        @staticmethod
        def request(
            method: str,
            params: object,
            *,
            deadline: float,
            cancellation=None,
        ):
            assert method == "workspace/symbol"
            if params == {"query": ""}:
                sent.set()
                events.append("empty-completed")
                return []
            assert params == {"query": "__llm_wiki_ownership_probe_no_match__"}
            return probe(deadline, cancellation)

    class Runtime(_RealNavigationRuntime):
        def __init__(self) -> None:
            self._cleanup_failed = False

        def _prepare_process(self, deadline: float):
            return object(), Process()

        def _reset(self, deadline: float) -> int:
            assert terminal.is_set()
            events.append("cleanup")
            return 0

    monkeypatch.setattr(ownership, "_OWNERSHIP_SCENARIOS", (scenario,))

    outcomes = Runtime().ownership_checks(deadline=time.monotonic() + 2.0)

    assert outcomes == {scenario: {"available": True, "orphan_count": 0}}
    assert sent.is_set()
    assert events == ["sent", "terminal", "cleanup"]


def test_ownership_waits_for_post_write_evidence_after_fast_completion() -> None:
    evidence_visible = threading.Event()
    events: list[str] = []
    timers: list[threading.Timer] = []

    class Protocol(_ProbeDeliveryProtocolShape):
        @staticmethod
        def _sent_request_evidence() -> tuple[int, str | None]:
            return (
                (1, "workspace/symbol")
                if evidence_visible.is_set()
                else (0, None)
            )

    class Process:
        protocol = Protocol()
        cancellation = None

        @classmethod
        def request(cls, *args, cancellation=None, **kwargs):
            cls.cancellation = cancellation
            events.append("completed")

            def publish_evidence() -> None:
                assert cancellation is not None
                assert cancellation.is_cancelled() is False
                events.append("evidence")
                evidence_visible.set()

            timer = threading.Timer(0.02, publish_evidence)
            timers.append(timer)
            timer.start()
            return []

    runtime = object.__new__(_RealNavigationRuntime)
    runtime._cleanup_failed = False
    try:
        with pytest.raises(
            ownership._ProbeRacedError,
            match="answered before interruption",
        ):
            runtime._observe_inflight_interruption(
                Process(),
                "cancellation",
                time.monotonic() + 1,
            )
    finally:
        for timer in timers:
            timer.join()

    assert evidence_visible.is_set()
    assert Process.cancellation is not None
    assert Process.cancellation.is_cancelled() is True
    assert events == ["completed", "evidence"]


def test_real_runtime_measures_four_active_ownership_scenarios() -> None:
    class ActivePopen:
        def __init__(self, runtime: ActiveOwnershipRuntime) -> None:
            self.runtime = runtime

        def kill(self) -> None:
            assert self.runtime.active
            self.runtime.active = False

        def wait(self, *, timeout: float) -> int:
            raise AssertionError("the lifecycle owner must reap the process")

    class ActiveProtocol(_ProbeDeliveryProtocolShape):
        def __init__(self, runtime: ActiveOwnershipRuntime) -> None:
            self.runtime = runtime

        def _sent_request_evidence(self) -> tuple[int, str | None]:
            return (
                (1, "workspace/symbol")
                if self.runtime.probe_sent
                else (0, None)
            )

    class ActiveProcess:
        def __init__(self, runtime: ActiveOwnershipRuntime) -> None:
            self.runtime = runtime
            self.process = ActivePopen(runtime)
            self.protocol = ActiveProtocol(runtime)

        def request(self, *args, deadline: float, cancellation=None, **kwargs):
            self.runtime.probe_sent = True
            if self.runtime.prepare_count == 4:
                assert cancellation is not None
                assert cancellation.wait(max(0.0, deadline - time.monotonic()))
                self.runtime.probe_terminal = True
                raise RequestCancelled("measured cancellation")
            while time.monotonic() < deadline:
                time.sleep(0.001)
            self.runtime.probe_terminal = True
            raise TimeoutError("measured timeout")

    class ActiveOwnershipRuntime(_RealNavigationRuntime):
        def __init__(self) -> None:
            self.active = False
            self.prepare_count = 0
            self.active_close_count = 0
            self.lazy_open_count = 0
            self.probe_sent = False
            self.probe_terminal = False
            self.process = ActiveProcess(self)

        def _prepare_process(self, deadline: float):
            assert deadline > time.monotonic()
            assert not self.active
            self.active = True
            self.prepare_count += 1
            self.probe_sent = False
            self.probe_terminal = False
            return object(), self.process

        def query(self, request: object, *, deadline: float) -> None:
            assert deadline > time.monotonic()
            self.active = True

        def _close_and_count(self, deadline: float) -> int:
            assert deadline > time.monotonic()
            assert self.active
            if self.prepare_count in {3, 4}:
                assert self.probe_terminal
            self.active = False
            self.active_close_count += 1
            return 0

        def _open(self) -> None:
            assert not self.active
            self.lazy_open_count += 1

    runtime = ActiveOwnershipRuntime()
    outcomes = runtime.ownership_checks(deadline=time.monotonic() + SHORT_TIMEOUT)

    assert tuple(outcomes) == (
        "normal_shutdown",
        "crash",
        "timeout",
        "cancellation",
    )
    assert all(outcome == {"available": True, "orphan_count": 0} for outcome in outcomes.values())
    assert runtime.prepare_count == 4
    assert runtime.active_close_count == 4
    assert runtime.lazy_open_count == 4
    assert runtime.active is False


@pytest.mark.parametrize(
    "first_close",
    [RuntimeError("first close failed"), 1],
    ids=["raises", "orphan-signal"],
)
def test_reset_retries_cleanup_once_and_never_reopens_after_failed_proof(
    first_close: object,
) -> None:
    class CleanupRuntime(_RealNavigationRuntime):
        def __init__(self) -> None:
            self.outcomes = [first_close, 0]
            self.close_deadlines: list[float] = []
            self.open_count = 0
            self._cleanup_failed = False

        def _close_and_count(self, deadline: float) -> int:
            self.close_deadlines.append(deadline)
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        def _open(self) -> None:
            self.open_count += 1

    runtime = CleanupRuntime()
    before = time.monotonic()
    with pytest.raises(RuntimeError, match="cleanup"):
        runtime._reset(before - 1.0)

    assert len(runtime.close_deadlines) == 2
    assert runtime.close_deadlines[1] > before
    assert runtime.open_count == 0
    assert runtime.cleanup_failed is True


def test_reset_retry_failure_retains_terminal_unknown_cleanup() -> None:
    class CleanupRuntime(_RealNavigationRuntime):
        def __init__(self) -> None:
            self.close_count = 0
            self.open_count = 0
            self._cleanup_failed = False

        def _close_and_count(self, deadline: float) -> int:
            self.close_count += 1
            raise RuntimeError("cleanup remains unproven")

        def _open(self) -> None:
            self.open_count += 1

    runtime = CleanupRuntime()
    with pytest.raises(RuntimeError, match="cleanup"):
        runtime._reset(time.monotonic() - 1.0)

    assert runtime.close_count == 2
    assert runtime.open_count == 0
    assert runtime.cleanup_failed is True


def test_ownership_cleanup_failure_stops_all_subsequent_scenarios() -> None:
    class Process:
        process = SimpleNamespace(kill=lambda: None, wait=lambda timeout: 0)

        @staticmethod
        def request(*args, **kwargs):
            raise TimeoutError("expected")

    class CleanupRuntime(_RealNavigationRuntime):
        def __init__(self) -> None:
            self.prepare_count = 0
            self.open_count = 0
            self.outcomes = [1, 0]
            self._cleanup_failed = False

        def _prepare_process(self, deadline: float):
            self.prepare_count += 1
            return object(), Process()

        def _close_and_count(self, deadline: float) -> int:
            return self.outcomes.pop(0)

        def _open(self) -> None:
            self.open_count += 1

    runtime = CleanupRuntime()
    outcomes = runtime.ownership_checks(deadline=time.monotonic() + SHORT_TIMEOUT)

    assert runtime.prepare_count == 1
    assert runtime.open_count == 0
    assert runtime.cleanup_failed is True
    assert all(
        outcome == {"available": False, "orphan_count": None}
        for outcome in outcomes.values()
    )


def test_close_counts_one_binary_incident_for_one_leaked_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_root = tmp_path / "owner"
    owner_root.mkdir()
    (owner_root / "lease.json").write_text("{}", encoding="utf-8")
    popen = SimpleNamespace(poll=lambda: None)
    process = SimpleNamespace(process=popen, _coordinator=object(), owner_root=owner_root)
    runtime = object.__new__(_RealNavigationRuntime)
    runtime._session = SimpleNamespace(_process=process)
    runtime._navigation = SimpleNamespace(close=lambda deadline: None)
    monkeypatch.setattr(ownership, "coordinator_has_ownership", lambda value: True)

    assert runtime._close_and_count(time.monotonic() + 10.0) == 1


def test_crash_without_a_process_returns_none() -> None:
    class NoProcessRuntime(_RealNavigationRuntime):
        def __init__(self) -> None:
            self._session = SimpleNamespace(_process=None)

        def query(self, request: object, *, deadline: float) -> None:
            return None

    assert (
        NoProcessRuntime().crash_and_recover(object(), deadline=time.monotonic() + SHORT_TIMEOUT)
        is None
    )


def test_post_crash_transient_not_ready_retries_once_under_the_same_deadline() -> None:
    events: list[str] = []
    deadlines: list[float] = []
    request = SimpleNamespace(
        capability=Capability.DEFINITIONS,
        repository=SimpleNamespace(repository_id="repository", checkout_id="checkout"),
        offset=0,
        limit=10,
    )
    recovered = replace(
        _empty_navigation_result(request),
        status=NavigationStatus.PARTIAL,
        readiness="query_ready",
    )
    transient = replace(
        _empty_navigation_result(request),
        status=NavigationStatus.NOT_READY,
        readiness="not_ready",
    )

    class Popen:
        def kill(self) -> None:
            events.append("kill")

        def wait(self, *, timeout: float) -> int:
            raise AssertionError("the lifecycle owner must reap the process")

    class CrashRuntime(_RealNavigationRuntime):
        def __init__(self) -> None:
            self._cleanup_failed = False
            self._session = SimpleNamespace(_process=SimpleNamespace(process=Popen()))
            self.query_results = [recovered, transient, recovered]

        def query(self, query_request: object, *, deadline: float) -> NavigationResult:
            assert query_request is request
            events.append("query")
            deadlines.append(deadline)
            return self.query_results.pop(0)

        def _reset(self, deadline: float) -> int:
            events.append("reset")
            return 0

    deadline = time.monotonic() + 10.0
    result = CrashRuntime().crash_and_recover(request, deadline=deadline)

    assert result is recovered
    assert events == ["query", "kill", "query", "query", "reset"]
    assert deadlines == [deadline, deadline, deadline]


def test_post_crash_transient_oserror_retries_once_under_the_same_deadline() -> None:
    events: list[str] = []
    deadlines: list[float] = []
    recovered = object()

    class Popen:
        def kill(self) -> None:
            events.append("kill")

        def wait(self, *, timeout: float) -> int:
            raise AssertionError("the lifecycle owner must reap the process")

    class CrashRuntime(_RealNavigationRuntime):
        def __init__(self) -> None:
            self._cleanup_failed = False
            self._session = SimpleNamespace(_process=SimpleNamespace(process=Popen()))
            self.query_calls = 0

        def query(self, request: object, *, deadline: float):
            self.query_calls += 1
            events.append(f"query-{self.query_calls}")
            deadlines.append(deadline)
            if self.query_calls == 2:
                raise OSError("lifecycle reaped the crashed process")
            return recovered

        def _reset(self, deadline: float) -> int:
            events.append("reset")
            return 0

    deadline = time.monotonic() + 10.0
    result = CrashRuntime().crash_and_recover(object(), deadline=deadline)

    assert result is recovered
    assert events == ["query-1", "kill", "query-2", "query-3", "reset"]
    assert deadlines == [deadline, deadline, deadline]


def test_post_crash_second_oserror_fails_closed_after_one_retry() -> None:
    events: list[str] = []

    class Popen:
        def kill(self) -> None:
            events.append("kill")

        def wait(self, *, timeout: float) -> int:
            raise AssertionError("the lifecycle owner must reap the process")

    class CrashRuntime(_RealNavigationRuntime):
        def __init__(self) -> None:
            self._cleanup_failed = False
            self._session = SimpleNamespace(_process=SimpleNamespace(process=Popen()))
            self.query_calls = 0

        def query(self, request: object, *, deadline: float):
            self.query_calls += 1
            events.append(f"query-{self.query_calls}")
            if self.query_calls > 1:
                raise OSError(f"recovery failure {self.query_calls - 1}")
            return object()

        def _reset(self, deadline: float) -> int:
            events.append("reset")
            return 0

    runtime = CrashRuntime()
    with pytest.raises(OSError, match="recovery failure 2"):
        runtime.crash_and_recover(object(), deadline=time.monotonic() + SHORT_TIMEOUT)

    assert runtime.query_calls == 3
    assert events == ["query-1", "kill", "query-2", "query-3", "reset"]


def test_post_crash_not_ready_retries_until_provider_recovers() -> None:
    request = SimpleNamespace(
        capability=Capability.DEFINITIONS,
        repository=SimpleNamespace(repository_id="repository", checkout_id="checkout"),
        offset=0,
        limit=10,
    )
    baseline = _empty_navigation_result(request)
    transient = replace(
        baseline,
        status=NavigationStatus.NOT_READY,
        readiness="not_ready",
    )
    initializing = replace(
        baseline,
        status=NavigationStatus.NOT_READY,
        readiness="protocol_initialized",
    )
    recovered = replace(
        baseline,
        status=NavigationStatus.PARTIAL,
        readiness="query_ready",
    )

    class Popen:
        def kill(self) -> None:
            return None

        def wait(self, *, timeout: float) -> int:
            raise AssertionError("the lifecycle owner must reap the process")

    class CrashRuntime(_RealNavigationRuntime):
        def __init__(self) -> None:
            self._cleanup_failed = False
            self._session = SimpleNamespace(_process=SimpleNamespace(process=Popen()))
            self.query_results = [
                recovered,
                transient,
                transient,
                initializing,
                transient,
                recovered,
            ]
            self.deadlines: list[float] = []
            self.reset_calls = 0

        def query(self, query_request: object, *, deadline: float) -> NavigationResult:
            self.deadlines.append(deadline)
            return self.query_results.pop(0)

        def _reset(self, deadline: float) -> int:
            self.reset_calls += 1
            return 0

    runtime = CrashRuntime()
    deadline = time.monotonic() + 10.0
    result = runtime.crash_and_recover(request, deadline=deadline)

    assert result is recovered
    assert runtime.query_results == []
    assert runtime.deadlines == [deadline] * 6
    assert runtime.reset_calls == 1


def test_post_crash_oserror_after_not_ready_uses_one_retry_budget() -> None:
    request = SimpleNamespace(
        capability=Capability.DEFINITIONS,
        repository=SimpleNamespace(repository_id="repository", checkout_id="checkout"),
        offset=0,
        limit=10,
    )
    baseline = _empty_navigation_result(request)
    transient = replace(
        baseline,
        status=NavigationStatus.NOT_READY,
        readiness="not_ready",
    )
    recovered = replace(
        baseline,
        status=NavigationStatus.PARTIAL,
        readiness="query_ready",
    )

    class Popen:
        def kill(self) -> None:
            return None

    class CrashRuntime(_RealNavigationRuntime):
        def __init__(self) -> None:
            self._cleanup_failed = False
            self._session = SimpleNamespace(_process=SimpleNamespace(process=Popen()))
            self.query_outcomes = [recovered, transient, OSError("reaped"), recovered]
            self.deadlines: list[float] = []
            self.reset_calls = 0

        def query(self, query_request: object, *, deadline: float) -> NavigationResult:
            self.deadlines.append(deadline)
            outcome = self.query_outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        def _reset(self, deadline: float) -> int:
            self.reset_calls += 1
            return 0

    runtime = CrashRuntime()
    deadline = time.monotonic() + 10.0

    result = runtime.crash_and_recover(request, deadline=deadline)

    assert result is recovered
    assert runtime.query_outcomes == []
    assert runtime.deadlines == [deadline] * 4
    assert runtime.reset_calls == 1


def test_post_crash_not_ready_stops_at_deadline_and_resets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = SimpleNamespace(
        capability=Capability.DEFINITIONS,
        repository=SimpleNamespace(repository_id="repository", checkout_id="checkout"),
        offset=0,
        limit=10,
    )
    baseline = _empty_navigation_result(request)
    transient = replace(
        baseline,
        status=NavigationStatus.NOT_READY,
        readiness="not_ready",
    )
    recovered = replace(
        baseline,
        status=NavigationStatus.PARTIAL,
        readiness="query_ready",
    )

    class Popen:
        def kill(self) -> None:
            return None

    class CrashRuntime(_RealNavigationRuntime):
        def __init__(self) -> None:
            self._cleanup_failed = False
            self._session = SimpleNamespace(_process=SimpleNamespace(process=Popen()))
            self.query_results = [baseline, transient, recovered]
            self.reset_deadlines: list[float] = []

        def query(self, query_request: object, *, deadline: float) -> NavigationResult:
            return self.query_results.pop(0)

        def _reset(self, deadline: float) -> int:
            self.reset_deadlines.append(deadline)
            return 0

    deadline = 10.0
    monkeypatch.setattr(ownership.time, "monotonic", lambda: deadline)
    runtime = CrashRuntime()

    result = runtime.crash_and_recover(request, deadline=deadline)

    assert result is transient
    assert runtime.query_results == [recovered]
    assert runtime.reset_deadlines == [deadline]


def test_post_crash_recovery_error_always_resets_and_later_cycles_continue() -> None:
    events: list[str] = []
    recovered = object()

    class Popen:
        def kill(self) -> None:
            events.append("kill")

        def wait(self, *, timeout: float) -> int:
            raise AssertionError("the lifecycle owner must reap the process")

    class CrashRuntime(_RealNavigationRuntime):
        def __init__(self) -> None:
            self._cleanup_failed = False
            self._session = SimpleNamespace(_process=SimpleNamespace(process=Popen()))
            self.query_calls = 0
            self.reset_calls = 0

        def query(self, request: object, *, deadline: float):
            self.query_calls += 1
            events.append(f"query-{self.query_calls}")
            if self.query_calls == 2:
                raise RuntimeError("recovery query failed")
            return recovered

        def _reset(self, deadline: float) -> int:
            self.reset_calls += 1
            events.append(f"reset-{self.reset_calls}")
            return 0

    runtime = CrashRuntime()
    deadline = time.monotonic() + 10.0
    with pytest.raises(RuntimeError, match="recovery query failed"):
        runtime.crash_and_recover(object(), deadline=deadline)

    assert runtime.reset_calls == 1
    assert events[:4] == ["query-1", "kill", "query-2", "reset-1"]
    assert runtime.cleanup_failed is False
    assert runtime.crash_and_recover(object(), deadline=deadline) is recovered
    assert runtime.reset_calls == 2


def test_post_crash_kill_error_cannot_bypass_reset() -> None:
    class Popen:
        def kill(self) -> None:
            raise OSError("intentional kill failed")

        def wait(self, *, timeout: float) -> int:
            raise AssertionError("the lifecycle owner must reap the process")

    class CrashRuntime(_RealNavigationRuntime):
        def __init__(self) -> None:
            self._cleanup_failed = False
            self._session = SimpleNamespace(_process=SimpleNamespace(process=Popen()))
            self.reset_calls = 0

        def query(self, request: object, *, deadline: float):
            return object()

        def _reset(self, deadline: float) -> int:
            self.reset_calls += 1
            return 0

    runtime = CrashRuntime()
    with pytest.raises(OSError, match="intentional kill failed"):
        runtime.crash_and_recover(object(), deadline=time.monotonic() + SHORT_TIMEOUT)
    assert runtime.reset_calls == 1


def test_post_crash_recovery_and_reset_failure_retries_then_becomes_terminal() -> None:
    class Popen:
        def kill(self) -> None:
            return None

        def wait(self, *, timeout: float) -> int:
            raise AssertionError("the lifecycle owner must reap the process")

    class CrashRuntime(_RealNavigationRuntime):
        def __init__(self) -> None:
            self._cleanup_failed = False
            self._session = SimpleNamespace(_process=SimpleNamespace(process=Popen()))
            self.query_calls = 0
            self.close_outcomes = [RuntimeError("first cleanup failed"), 0]
            self.close_deadlines: list[float] = []
            self.open_count = 0

        def query(self, request: object, *, deadline: float):
            self.query_calls += 1
            if self.query_calls == 2:
                raise RuntimeError("recovery query failed")
            return object()

        def _close_and_count(self, deadline: float) -> int:
            self.close_deadlines.append(deadline)
            outcome = self.close_outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        def _open(self) -> None:
            self.open_count += 1

    runtime = CrashRuntime()
    operation_deadline = time.monotonic() + 1.0
    with pytest.raises(RuntimeError, match="cleanup"):
        runtime.crash_and_recover(object(), deadline=operation_deadline)

    assert len(runtime.close_deadlines) == 2
    assert runtime.close_deadlines[1] > operation_deadline
    assert runtime.open_count == 0
    assert runtime.cleanup_failed is True
    calls = runtime.query_calls
    assert runtime.crash_and_recover(object(), deadline=time.monotonic() + SHORT_TIMEOUT) is None
    assert runtime.query_calls == calls


def test_a_raced_ownership_probe_is_retried_until_it_measures_something() -> None:
    """A server that answers first measured nothing; the next attempt may not."""
    attempts: list[str] = []
    runtime = object.__new__(_RealNavigationRuntime)
    runtime._cleanup_failed = False

    def run(scenario: str, deadline: float) -> int:
        attempts.append(scenario)
        if len(attempts) < 3:
            raise ownership._ProbeRacedError("answered before interruption")
        return 0

    runtime._run_ownership_scenario = run
    runtime._reset = lambda deadline: 0
    outcome = runtime._ownership_outcome("cancellation", time.monotonic() + 60)
    assert outcome == {"available": True, "orphan_count": 0}
    assert attempts == ["cancellation"] * 3


def test_an_always_raced_ownership_probe_reports_the_scenario_unavailable() -> None:
    """The retry is bounded, so a server that always wins is still reported."""
    attempts: list[str] = []
    runtime = object.__new__(_RealNavigationRuntime)
    runtime._cleanup_failed = False

    def run(scenario: str, deadline: float) -> int:
        attempts.append(scenario)
        raise ownership._ProbeRacedError("answered before interruption")

    runtime._run_ownership_scenario = run
    runtime._reset = lambda deadline: 0
    outcome = runtime._ownership_outcome("cancellation", time.monotonic() + 60)
    assert outcome == {"available": False, "orphan_count": None}
    assert len(attempts) == ownership._OWNERSHIP_PROBE_ATTEMPTS


def test_a_failing_ownership_probe_is_not_retried() -> None:
    """A real failure is a finding, not a race; one attempt settles it."""
    attempts: list[str] = []
    runtime = object.__new__(_RealNavigationRuntime)
    runtime._cleanup_failed = False

    def run(scenario: str, deadline: float) -> int:
        attempts.append(scenario)
        raise RuntimeError("ownership probe reached the wrong terminal")

    runtime._run_ownership_scenario = run
    runtime._reset = lambda deadline: 0
    outcome = runtime._ownership_outcome("cancellation", time.monotonic() + 60)
    assert outcome == {"available": False, "orphan_count": None}
    assert attempts == ["cancellation"]
