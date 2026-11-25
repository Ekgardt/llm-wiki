"""One probe answers `alive | dead | unknown`; doubt never reads as dead."""

from __future__ import annotations

import errno
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import process_liveness  # noqa: E402


def test_our_own_process_is_alive() -> None:
    assert process_liveness.process_state(os.getpid()) == "alive"
    assert process_liveness.pid_alive(os.getpid()) is True


@pytest.mark.parametrize("pid", [0, -1, True, "12", None])
def test_a_pid_that_is_not_a_positive_integer_is_unknown_and_never_dead(pid) -> None:
    assert process_liveness.process_state(pid) == "unknown"
    assert process_liveness.pid_alive(pid) is True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX kill(2) semantics")
def test_esrch_is_dead_and_eperm_is_alive_in_doubt(monkeypatch) -> None:
    def refuse(_pid, _signal):
        raise PermissionError(errno.EPERM, "operation not permitted")

    monkeypatch.setattr(process_liveness.os, "kill", refuse)
    assert process_liveness.process_state(4242) == "unknown"
    assert process_liveness.pid_alive(4242) is True

    def gone(_pid, _signal):
        raise ProcessLookupError(errno.ESRCH, "no such process")

    monkeypatch.setattr(process_liveness.os, "kill", gone)
    assert process_liveness.process_state(4242) == "dead"
    assert process_liveness.pid_alive(4242) is False


def test_the_three_legacy_locks_ask_the_same_probe(monkeypatch) -> None:
    """Audit OPS-08: memory_state, markdown_transaction and doctor delegate."""
    import doctor
    import markdown_transaction
    import memory_state

    monkeypatch.setattr(process_liveness, "process_state", lambda _pid: "unknown")

    assert memory_state._is_pid_alive(4242) is True
    assert markdown_transaction._pid_alive(4242) is True
    assert doctor._pid_alive(4242) is True
    assert doctor._lsp_pid_state(4242) == "unknown"


def test_linux_identity_binds_boot_id_and_start_ticks(monkeypatch) -> None:
    stat = b"42 (worker name) S " + b" ".join(str(value).encode() for value in range(1, 23))

    def read(path: Path, _maximum: int) -> bytes:
        if path == Path("/proc/42/stat"):
            return stat
        return b"550e8400-e29b-41d4-a716-446655440000\n"

    monkeypatch.setattr(process_liveness, "_read_bounded_system_file", read)

    assert process_liveness._linux_process_start_identity(42) == (
        "linux:550e8400-e29b-41d4-a716-446655440000:19"
    )


@pytest.mark.parametrize(
    ("system", "probe", "expected"),
    [
        ("Linux", "_linux_process_start_identity", "linux:boot:77"),
        ("Darwin", "_darwin_process_start_identity", "darwin:1:77"),
        ("Windows", "_windows_process_start_identity", "windows:77"),
    ],
)
def test_each_platform_answers_with_its_own_probe(
    monkeypatch, system: str, probe: str, expected: str
) -> None:
    monkeypatch.setattr(process_liveness, "_platform_system", lambda: system)
    monkeypatch.setattr(process_liveness, probe, lambda pid: f"{expected}:{pid}")

    assert process_liveness.process_start_identity(77) == f"{expected}:77"


def test_a_platform_without_a_probe_refuses_by_name(monkeypatch) -> None:
    monkeypatch.setattr(process_liveness, "_platform_system", lambda: "Plan9")

    with pytest.raises(process_liveness.ProcessIdentityUnavailable):
        process_liveness.process_start_identity(77)


def _darwin_information(status: int):
    """What `proc_pidinfo` writes for a process in this state."""

    def fill(_pid, information, size):
        information.pid = 4242
        information.status = status
        information.start_seconds = 1_700_000_000
        information.start_microseconds = 5
        return size

    return fill


def test_a_darwin_zombie_is_a_missing_process_not_an_unsettled_probe(monkeypatch) -> None:
    """Q-L8: with `arg=1` the kernel finds the zombie, and SZOMB reads as gone."""
    monkeypatch.setattr(process_liveness, "_platform_system", lambda: "Darwin")
    monkeypatch.setattr(
        process_liveness,
        "_darwin_proc_pidinfo",
        _darwin_information(process_liveness._DARWIN_SZOMB),
    )

    assert process_liveness.process_start_identity(4242) is None
    assert process_liveness.owner_alive(4242, "darwin:1700000000:5") is False


def test_a_darwin_process_that_runs_keeps_its_start_identity(monkeypatch) -> None:
    monkeypatch.setattr(process_liveness, "_platform_system", lambda: "Darwin")
    monkeypatch.setattr(process_liveness, "_darwin_proc_pidinfo", _darwin_information(2))

    assert process_liveness.process_start_identity(4242) == "darwin:1700000000:5"


def test_a_reused_pid_is_dead_to_the_owner_that_recorded_its_identity(monkeypatch) -> None:
    monkeypatch.setattr(process_liveness, "process_start_identity", lambda _pid: "linux:boot:99")

    assert process_liveness.owner_alive(4242, "linux:boot:12") is False
    assert process_liveness.owner_alive(4242, "linux:boot:99") is True


def test_without_a_recorded_identity_the_answer_is_the_pid_probe(monkeypatch) -> None:
    """A lock written before this release still reads exactly as it did."""
    monkeypatch.setattr(process_liveness, "process_state", lambda _pid: "dead")

    assert process_liveness.owner_alive(4242) is False
    assert process_liveness.owner_alive(4242, "") is False


def test_an_unsettled_probe_leaves_the_owner_alive(monkeypatch) -> None:
    def refuse(_pid):
        raise PermissionError(errno.EPERM, "operation not permitted")

    monkeypatch.setattr(process_liveness, "process_start_identity", refuse)

    assert process_liveness.owner_alive(4242, "linux:boot:12") is True


def test_our_own_process_matches_the_identity_it_reports() -> None:
    identity = process_liveness.process_start_identity(os.getpid())

    assert process_liveness.owner_alive(os.getpid(), identity) is True


def test_a_pid_beyond_the_platform_range_is_never_alive_by_guess() -> None:
    """Windows answers `dead` (error 87) for 2**40; POSIX raises OverflowError → unknown.
    Either way the one boolean the locks read is the honest one."""
    state = process_liveness.process_state(2**40)
    assert state in {"dead", "unknown"}
    assert process_liveness.pid_alive(2**40) is (state != "dead")
