from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import cast

import pytest
from llm_wiki import lsp_process_requests, lsp_process_state
from llm_wiki.lsp_process_state import (
    FailureEvidenceIdentity,
    Generation,
    LifecycleCoordinator,
    LifecyclePhase,
    LspProcessHandle,
    ProcessState,
)
from lsp_protocol import LocalRequestViolation, LspProtocol, ProtocolViolation

FIRST_NONCE = "a" * 32
SECOND_NONCE = "b" * 32
WAIT_SECONDS = 30.0


class FakeServerProcess:
    def __init__(self, exit_code: int | None = None) -> None:
        self.exit_code = exit_code

    def poll(self) -> int | None:
        return self.exit_code


def _answer_ok() -> object:
    return {"answer": "ok"}


def _no_side_effect() -> None:
    return None


@dataclass
class ScriptedProtocol:
    fatal: bool = False
    expired: tuple[tuple[str, int], ...] = ()
    respond: Callable[[], object] = _answer_ok
    before_notify: Callable[[], None] = _no_side_effect
    requests: list[str] = field(default_factory=list)
    notifications: list[str] = field(default_factory=list)

    def expired_drain_keys(self, now: float) -> tuple[tuple[str, int], ...]:
        return self.expired

    def request(
        self, method: str, params: object, *, deadline: float, cancellation: object
    ) -> object:
        self.requests.append(method)
        return self.respond()

    def notify(self, method: str, params: object, *, deadline: float) -> None:
        self.before_notify()
        self.notifications.append(method)


@dataclass
class FakeLspInstance:
    _coordinator: LifecycleCoordinator
    generation_nonce: str
    state: ProcessState = ProcessState.PROTOCOL_INITIALIZED
    last_used_monotonic: float = 0.0


def _generation(
    nonce: str, protocol: ScriptedProtocol, process: FakeServerProcess | None = None
) -> Generation:
    generation = Generation(nonce, None, None)
    generation.protocol = cast(LspProtocol, protocol)
    generation.process = cast(object, process or FakeServerProcess())
    return generation


def _running(generation: Generation) -> tuple[FakeLspInstance, LspProcessHandle]:
    coordinator = LifecycleCoordinator(owner_directory=None)
    coordinator.phase = LifecyclePhase.RUNNING
    coordinator.active = generation
    instance = FakeLspInstance(coordinator, generation.nonce)
    return instance, cast(LspProcessHandle, instance)


def _deadline() -> float:
    return time.monotonic() + WAIT_SECONDS


def _replace_active_on_failure(
    coordinator: LifecycleCoordinator, replacement: Generation
) -> threading.Thread:
    def recover(current: LifecycleCoordinator) -> None:
        current.active = replacement

    return _recovery_owner(coordinator, recover)


def _fail_restart_on_failure(
    coordinator: LifecycleCoordinator, terminal_code: str
) -> threading.Thread:
    def give_up(current: LifecycleCoordinator) -> None:
        current.phase = LifecyclePhase.STOPPED_FAILURE
        current.terminal_outcome = "failure"
        current.terminal_code = terminal_code
        lsp_process_state.remember_background_restart_error(
            current, PermissionError(13, "replacement was refused")
        )

    return _recovery_owner(coordinator, give_up)


def _recovery_owner(
    coordinator: LifecycleCoordinator, *settles: Callable[[LifecycleCoordinator], None]
) -> threading.Thread:
    def run() -> None:
        for settle in settles:
            _settle_on_wake(coordinator, settle)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def _settle_on_wake(
    coordinator: LifecycleCoordinator, settle: Callable[[LifecycleCoordinator], None]
) -> None:
    coordinator.recovery_wake.wait(WAIT_SECONDS)
    coordinator.recovery_wake.clear()
    with coordinator.condition:
        settle(coordinator)
        coordinator.condition.notify_all()


def _request(handle: LspProcessHandle) -> object:
    return lsp_process_requests.request_lsp_process(
        handle, "textDocument/hover", {}, deadline=_deadline(), cancellation=None
    )


def test_a_request_is_answered_by_the_active_generation() -> None:
    protocol = ScriptedProtocol()
    instance, handle = _running(_generation(FIRST_NONCE, protocol))

    assert _request(handle) == {"answer": "ok"}
    assert protocol.requests == ["textDocument/hover"]
    assert instance.last_used_monotonic > 0


def test_a_request_waits_out_a_transition_and_times_out_if_it_never_ends() -> None:
    instance, handle = _running(_generation(FIRST_NONCE, ScriptedProtocol()))
    instance._coordinator.phase = LifecyclePhase.RESTARTING

    with pytest.raises(TimeoutError, match="transition did not finish"):
        lsp_process_requests.request_lsp_process(
            handle, "m", {}, deadline=time.monotonic() + 0.05, cancellation=None
        )


@pytest.mark.parametrize(
    ("state", "message"),
    [(ProcessState.FAILED, "has exited"), (ProcessState.WORKSPACE_READY, "is closed")],
)
def test_a_finished_lifecycle_refuses_requests(state: ProcessState, message: str) -> None:
    instance, handle = _running(_generation(FIRST_NONCE, ScriptedProtocol()))
    instance.state = state
    instance._coordinator.phase = LifecyclePhase.STOPPED_SUCCESS

    with pytest.raises(RuntimeError, match=message):
        _request(handle)


def test_a_running_lifecycle_without_a_generation_refuses_requests() -> None:
    instance, handle = _running(_generation(FIRST_NONCE, ScriptedProtocol()))
    instance._coordinator.active = None

    with pytest.raises(RuntimeError, match="generation is unavailable"):
        _request(handle)


def test_an_expired_drain_hands_the_request_to_the_replacement_generation() -> None:
    stale = _generation(FIRST_NONCE, ScriptedProtocol(expired=(("$/progress", 1),)))
    fresh_protocol = ScriptedProtocol()
    fresh = _generation(SECOND_NONCE, fresh_protocol)
    instance, handle = _running(stale)
    recovery = _replace_active_on_failure(instance._coordinator, fresh)

    assert _request(handle) == {"answer": "ok"}
    recovery.join(WAIT_SECONDS)
    assert stale.failure_queued
    assert fresh_protocol.requests == ["textDocument/hover"]


def test_an_exited_server_whose_replacement_failed_reports_the_restart_cause() -> None:
    exited = _generation(FIRST_NONCE, ScriptedProtocol(), FakeServerProcess(exit_code=1))
    instance, handle = _running(exited)
    recovery = _fail_restart_on_failure(instance._coordinator, "restart_failed")

    with pytest.raises(ProtocolViolation, match="replacement startup failed") as raised:
        _request(handle)
    recovery.join(WAIT_SECONDS)
    assert isinstance(raised.value.__cause__, RuntimeError)
    assert "PermissionError" in str(raised.value.__cause__)


def test_a_replacement_failure_without_a_recorded_cause_has_no_cause() -> None:
    exited = _generation(FIRST_NONCE, ScriptedProtocol(fatal=True))
    instance, handle = _running(exited)

    def give_up_silently(current: LifecycleCoordinator) -> None:
        current.phase = LifecyclePhase.STOPPED_FAILURE
        current.terminal_code = "restart_failed"

    recovery = _recovery_owner(instance._coordinator, give_up_silently)
    with pytest.raises(ProtocolViolation, match="replacement startup failed") as raised:
        _request(handle)
    recovery.join(WAIT_SECONDS)
    assert raised.value.__cause__ is None


def test_an_exited_server_that_ended_the_lifecycle_refuses_the_request() -> None:
    exited = _generation(FIRST_NONCE, ScriptedProtocol(), FakeServerProcess(exit_code=1))
    instance, handle = _running(exited)
    recovery = _fail_restart_on_failure(instance._coordinator, "process_exited")

    with pytest.raises(ProtocolViolation, match="fatally unavailable"):
        _request(handle)
    recovery.join(WAIT_SECONDS)


def test_a_violation_on_a_healthy_generation_is_the_callers_error() -> None:
    def refuse() -> object:
        raise ProtocolViolation("response refused")

    protocol = ScriptedProtocol(respond=refuse)
    generation = _generation(FIRST_NONCE, protocol)
    _, handle = _running(generation)

    with pytest.raises(ProtocolViolation, match="response refused"):
        _request(handle)
    assert not generation.failure_queued


def test_a_local_request_violation_never_reaches_recovery() -> None:
    def refuse_locally() -> object:
        raise LocalRequestViolation("bad params")

    generation = _generation(FIRST_NONCE, ScriptedProtocol(respond=refuse_locally))
    _, handle = _running(generation)

    with pytest.raises(LocalRequestViolation):
        _request(handle)
    assert not generation.failure_queued


def test_a_violation_from_a_dying_server_is_retried_on_its_replacement() -> None:
    protocol = ScriptedProtocol()

    def die_mid_request() -> object:
        protocol.fatal = True
        raise ProtocolViolation("connection lost")

    protocol.respond = die_mid_request
    stale = _generation(FIRST_NONCE, protocol)
    fresh = _generation(SECOND_NONCE, ScriptedProtocol())
    instance, handle = _running(stale)
    recovery = _replace_active_on_failure(instance._coordinator, fresh)

    assert _request(handle) == {"answer": "ok"}
    recovery.join(WAIT_SECONDS)
    assert stale.failure_queued


def _protocol_dying_mid_request() -> ScriptedProtocol:
    protocol = ScriptedProtocol()

    def die_mid_request() -> object:
        protocol.fatal = True
        raise ProtocolViolation("connection lost")

    protocol.respond = die_mid_request
    return protocol


def test_a_second_dying_generation_ends_the_request() -> None:
    stale = _generation(FIRST_NONCE, _protocol_dying_mid_request())
    also_dying = _generation(SECOND_NONCE, _protocol_dying_mid_request())
    instance, handle = _running(stale)

    def replace(current: LifecycleCoordinator) -> None:
        current.active = also_dying

    def end_lifecycle(current: LifecycleCoordinator) -> None:
        current.phase = LifecyclePhase.STOPPED_FAILURE

    recovery = _recovery_owner(instance._coordinator, replace, end_lifecycle)
    with pytest.raises(ProtocolViolation, match="connection lost"):
        _request(handle)
    recovery.join(WAIT_SECONDS)
    assert stale.failure_queued
    assert also_dying.failure_queued


def _promote(handle: LspProcessHandle, nonce: str = FIRST_NONCE) -> bool:
    return lsp_process_requests.promote_lsp_process_workspace_ready(
        handle, generation_nonce=nonce, deadline=_deadline()
    )


def test_a_healthy_running_generation_is_promoted_to_workspace_ready() -> None:
    instance, handle = _running(_generation(FIRST_NONCE, ScriptedProtocol()))

    assert _promote(handle)
    assert instance.state is ProcessState.WORKSPACE_READY


def _phase_starting(instance: FakeLspInstance, generation: Generation) -> None:
    instance._coordinator.phase = LifecyclePhase.STARTING


def _outcome_failure(instance: FakeLspInstance, generation: Generation) -> None:
    instance._coordinator.terminal_outcome = "failure"


def _instance_on_other_generation(instance: FakeLspInstance, generation: Generation) -> None:
    instance.generation_nonce = SECOND_NONCE


def _active_is_other_generation(instance: FakeLspInstance, generation: Generation) -> None:
    instance._coordinator.active = _generation(SECOND_NONCE, ScriptedProtocol())


def _no_active_generation(instance: FakeLspInstance, generation: Generation) -> None:
    instance._coordinator.active = None


def _channel_dropped(instance: FakeLspInstance, generation: Generation) -> None:
    generation.protocol = None


def _exit_observed(instance: FakeLspInstance, generation: Generation) -> None:
    generation._exit_observed = True


def _expected_exit(instance: FakeLspInstance, generation: Generation) -> None:
    generation.expected_exit.set()


def _protocol_fatal(instance: FakeLspInstance, generation: Generation) -> None:
    cast(ScriptedProtocol, generation.protocol).fatal = True


def _success_committed(instance: FakeLspInstance, generation: Generation) -> None:
    instance._coordinator.success_committed = True


def _mandatory_failure(instance: FakeLspInstance, generation: Generation) -> None:
    instance._coordinator.mandatory_failure_intent = FailureEvidenceIdentity(
        "process_exited", "c" * 32, FIRST_NONCE, None
    )


def _failure_pending(instance: FakeLspInstance, generation: Generation) -> None:
    instance._coordinator.pending_failure_intents = 1


@pytest.mark.parametrize(
    "spoil",
    [
        _phase_starting,
        _outcome_failure,
        _instance_on_other_generation,
        _active_is_other_generation,
        _no_active_generation,
        _channel_dropped,
        _exit_observed,
        _expected_exit,
        _protocol_fatal,
        _success_committed,
        _mandatory_failure,
        _failure_pending,
    ],
)
def test_promotion_is_refused_unless_the_named_generation_is_live_and_unending(
    spoil: Callable[[FakeLspInstance, Generation], None],
) -> None:
    generation = _generation(FIRST_NONCE, ScriptedProtocol())
    instance, handle = _running(generation)
    spoil(instance, generation)

    assert not _promote(handle)
    assert instance.state is ProcessState.PROTOCOL_INITIALIZED


@pytest.mark.parametrize(
    ("nonce", "error", "message"),
    [
        (7, TypeError, "must be a string"),
        ("A" * 32, ValueError, "32 lowercase hexadecimal"),
    ],
)
def test_promotion_validates_the_generation_nonce(
    nonce: object, error: type[Exception], message: str
) -> None:
    _, handle = _running(_generation(FIRST_NONCE, ScriptedProtocol()))

    with pytest.raises(error, match=message):
        _promote(handle, cast(str, nonce))


def test_waiting_for_a_generation_change_times_out_on_an_unchanged_generation() -> None:
    _, handle = _running(_generation(FIRST_NONCE, ScriptedProtocol()))

    with pytest.raises(TimeoutError, match="recovery did not finish"):
        lsp_process_requests.wait_for_generation_change(
            handle, FIRST_NONCE, time.monotonic() + 0.05
        )


def test_waiting_for_a_generation_change_reports_the_new_generation() -> None:
    _, handle = _running(_generation(SECOND_NONCE, ScriptedProtocol()))

    assert lsp_process_requests.wait_for_generation_change(handle, FIRST_NONCE, _deadline()) == (
        True,
        None,
    )


def _notify(handle: LspProcessHandle) -> None:
    lsp_process_requests.notify_lsp_process(
        handle, "textDocument/didOpen", {}, deadline=_deadline()
    )


def test_a_notification_reaches_the_active_generation() -> None:
    protocol = ScriptedProtocol()
    _, handle = _running(_generation(FIRST_NONCE, protocol))

    _notify(handle)

    assert protocol.notifications == ["textDocument/didOpen"]


def test_a_notification_to_an_exited_server_queues_its_failure() -> None:
    generation = _generation(FIRST_NONCE, ScriptedProtocol(), FakeServerProcess(exit_code=0))
    _, handle = _running(generation)

    with pytest.raises(ProtocolViolation, match="fatally unavailable"):
        _notify(handle)
    assert generation.failure_queued


def _dying_notify_generation(drop_channel: bool) -> Generation:
    protocol = ScriptedProtocol()
    generation = _generation(FIRST_NONCE, protocol)

    def die() -> None:
        protocol.fatal = True
        if drop_channel:
            generation.protocol = None
        raise ProtocolViolation("pipe closed")

    protocol.before_notify = die
    return generation


@pytest.mark.parametrize(("drop_channel", "queued"), [(False, True), (True, False)])
def test_a_violating_notification_queues_failure_only_while_the_channel_is_held(
    drop_channel: bool, queued: bool
) -> None:
    generation = _dying_notify_generation(drop_channel)
    _, handle = _running(generation)

    with pytest.raises(ProtocolViolation, match="pipe closed"):
        _notify(handle)
    assert generation.failure_queued is queued


def _notify_generation(handle: LspProcessHandle, nonce: str = FIRST_NONCE) -> bool:
    return lsp_process_requests.notify_lsp_process_generation(
        handle, "workspace/didChangeConfiguration", {}, generation_nonce=nonce, deadline=_deadline()
    )


def test_a_generation_notification_is_sent_to_its_named_generation() -> None:
    protocol = ScriptedProtocol()
    _, handle = _running(_generation(FIRST_NONCE, protocol))

    assert _notify_generation(handle)
    assert protocol.notifications == ["workspace/didChangeConfiguration"]


def test_a_generation_notification_for_another_generation_is_not_sent() -> None:
    protocol = ScriptedProtocol()
    _, handle = _running(_generation(FIRST_NONCE, protocol))

    assert not _notify_generation(handle, SECOND_NONCE)
    assert protocol.notifications == []


def test_a_generation_notification_to_an_unusable_generation_queues_its_failure() -> None:
    generation = _generation(FIRST_NONCE, ScriptedProtocol(expired=(("$/x", 2),)))
    _, handle = _running(generation)

    assert not _notify_generation(handle)
    assert generation.failure_queued


@pytest.mark.parametrize(("drop_channel", "queued"), [(False, True), (True, False)])
def test_a_violating_generation_notification_is_raised_and_recorded(
    drop_channel: bool, queued: bool
) -> None:
    generation = _dying_notify_generation(drop_channel)
    _, handle = _running(generation)

    with pytest.raises(ProtocolViolation, match="pipe closed"):
        _notify_generation(handle)
    assert generation.failure_queued is queued


def _failure_queued(instance: FakeLspInstance, generation: Generation) -> None:
    generation.failure_queued = True


@pytest.mark.parametrize(
    "spoil",
    [
        _exit_observed,
        _failure_queued,
        _expected_exit,
        _success_committed,
        _mandatory_failure,
        _failure_pending,
        _instance_on_other_generation,
    ],
)
def test_a_generation_notification_is_withheld_from_an_ending_generation(
    spoil: Callable[[FakeLspInstance, Generation], None],
) -> None:
    protocol = ScriptedProtocol()
    generation = _generation(FIRST_NONCE, protocol)
    instance, handle = _running(generation)
    spoil(instance, generation)

    assert not _notify_generation(handle)
    assert protocol.notifications == []


def _close_lands(instance: FakeLspInstance, generation: Generation) -> None:
    instance._coordinator.terminal_outcome = "success"


def _replacement_lands(instance: FakeLspInstance, generation: Generation) -> None:
    instance._coordinator.active = _generation(FIRST_NONCE, ScriptedProtocol())


@pytest.mark.parametrize("land", [_close_lands, _replacement_lands, _channel_dropped])
def test_a_change_between_choosing_and_notifying_withholds_the_notification(
    monkeypatch: pytest.MonkeyPatch,
    land: Callable[[FakeLspInstance, Generation], None],
) -> None:
    protocol = ScriptedProtocol()
    generation = _generation(FIRST_NONCE, protocol)
    instance, handle = _running(generation)
    choose = lsp_process_requests.request_generation

    def choose_then_change(
        current: LspProcessHandle, deadline: float
    ) -> lsp_process_requests._GenerationChannel:
        channel = choose(current, deadline)
        land(instance, generation)
        return channel

    monkeypatch.setattr(lsp_process_requests, "request_generation", choose_then_change)

    assert not _notify_generation(handle)
    assert protocol.notifications == []
