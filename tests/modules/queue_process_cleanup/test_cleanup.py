"""Cleanup outcomes, including unknown trees and surviving descendants."""
from __future__ import annotations

import os
import signal
import subprocess

import llm_wiki.queue_process_cleanup as cleanup
import pytest


class _FakeProcess:
    def __init__(self, *, stubborn: bool = False) -> None:
        self.pid = 4242
        self.alive = True
        self.stubborn = stubborn
        self.terminated = 0
        self.killed = 0
        self.exitcode = None

    def is_alive(self) -> bool:
        return self.alive

    def join(self, timeout=None) -> None:
        del timeout

    def terminate(self) -> None:
        self.terminated += 1
        if not self.stubborn:
            self.alive = False

    def kill(self) -> None:
        self.killed += 1
        if not self.stubborn:
            self.alive = False


def test_windows_tree_cleanup_failure_falls_back_and_reports_unverified_descendants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess()
    monkeypatch.setattr(
        cleanup.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1),
    )
    monkeypatch.setattr(cleanup, "tracked_descendant_pids", lambda *args: None)

    with pytest.raises(cleanup.QueueOperationError) as raised:
        cleanup.terminate_processor_child(process, platform_name="nt")

    assert raised.value.code == "process_cleanup_failed"
    assert raised.value.detail == "snapshot_known=False; tree_verified=False; termination_verified=False"
    assert process.terminated == 1
    assert not process.is_alive()


def test_windows_tree_cleanup_verifies_direct_child_liveness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess(stubborn=True)
    monkeypatch.setattr(
        cleanup.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0),
    )
    monkeypatch.setattr(cleanup, "tracked_descendant_pids", lambda *args: set())

    with pytest.raises(cleanup.QueueOperationError) as raised:
        cleanup.terminate_processor_child(process, platform_name="nt")

    assert raised.value.code == "process_cleanup_failed"
    assert process.terminated == 1
    assert process.killed == 1


@pytest.mark.skipif(
    not hasattr(os, "killpg"),
    reason="the POSIX group path needs process groups, which this host has not",
)
def test_posix_group_race_uses_direct_fallback_and_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A vanished group is the outcome the kill wanted, not an unverified tree.

    This asserted `process_cleanup_failed`, and that verdict was inconsistent
    with the same exception one function earlier: `_kill_posix_group` already
    treats a vanished group with no descendants as verified, and then
    `hard_kill_group` un-verified it. Nothing was lost by removing the
    refusal, because the group question is answered directly and
    independently: `_cleanup_confirmed` requires the child gone, every tracked
    descendant gone, and `process_group_alive` false — a live /proc scan of
    the group, not an inference from a signal's errno. A descendant that
    escaped into another group is invisible to both, with or without the
    refusal.

    The refusal that matters is still pinned, by
    `test_cleanup_fails_when_tracked_descendant_remains_alive`.
    """
    process = _FakeProcess()
    monkeypatch.setattr(
        cleanup,
        "kill_process_group",
        lambda *args: (_ for _ in ()).throw(ProcessLookupError()),
    )
    monkeypatch.setattr(cleanup, "tracked_descendant_pids", lambda *args: set())

    cleanup.terminate_processor_child(process, platform_name="posix")

    assert process.terminated == 1
    assert not process.is_alive()


def test_posix_group_cleanup_waits_and_verifies_process_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess()

    def kill_group(pid: int, sig: int) -> None:
        assert pid == process.pid
        if sig == signal.SIGTERM:
            process.alive = False

    monkeypatch.setattr(cleanup, "kill_process_group", kill_group)
    monkeypatch.setattr(cleanup, "tracked_descendant_pids", lambda *args: set())
    monkeypatch.setattr(cleanup, "process_group_alive", lambda pid: False)

    cleanup.terminate_processor_child(process, platform_name="posix")

    assert not process.is_alive()


def test_cleanup_fails_when_tracked_descendant_remains_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess()
    descendant = 7777
    monkeypatch.setattr(
        cleanup.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0),
    )
    monkeypatch.setattr(
        cleanup, "tracked_descendant_pids", lambda *args: {descendant}
    )
    monkeypatch.setattr(
        cleanup, "pid_is_alive", lambda pid: pid == descendant
    )

    with pytest.raises(cleanup.QueueOperationError) as raised:
        cleanup.terminate_processor_child(
            process, platform_name="nt", cleanup_timeout=0.01
        )

    assert raised.value.code == "process_cleanup_failed"
    assert raised.value.detail == "snapshot_known=True; tree_verified=True; termination_verified=False"


def test_cleanup_waits_until_tracked_descendant_is_confirmed_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess()
    descendant = 8888
    checks = iter((True, True, False))
    monkeypatch.setattr(
        cleanup.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0),
    )
    monkeypatch.setattr(
        cleanup, "tracked_descendant_pids", lambda *args: {descendant}
    )
    monkeypatch.setattr(
        cleanup,
        "pid_is_alive",
        lambda pid: next(checks) if pid == descendant else False,
    )

    cleanup.terminate_processor_child(
        process, platform_name="nt", cleanup_timeout=1
    )

    assert not process.is_alive()


def test_cleanup_fails_closed_when_windows_child_exits_before_tracking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess()
    process.alive = False
    monkeypatch.setattr(cleanup, "tracked_descendant_pids", lambda *args: set())

    with pytest.raises(cleanup.QueueOperationError) as raised:
        cleanup.terminate_processor_child(process, platform_name="nt")

    assert raised.value.code == "process_cleanup_failed"

