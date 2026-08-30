"""A group that SIGTERM already killed is not a failed SIGKILL.

`_hard_kill_group` follows the group's SIGTERM with a SIGKILL. When the group
has already gone — which is what the SIGTERM was for — `os.killpg` raises
`ProcessLookupError`, and that was recorded as an unverified tree. On POSIX an
unverified tree makes `_cleanup_failed` refuse even after `_await_cleanup` has
confirmed the child and every descendant are gone, so a clean timeout was
reported as `process_cleanup_failed`.

Traced on the live machine 2026-08-29, paired at load 16-17: before the change
a worker timeout of 0.05 s and of 0.2 s both raised
`process_cleanup_failed`; after it, both raise the expected `TimeoutError`.
On a quiet machine the group is still there when the SIGKILL lands and nothing
fails, which is why
`test_worker_timeout_kills_spawned_grandchild_tree` only flaked under load.
"""

from __future__ import annotations

import signal
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import memory_queue  # noqa: E402

# Every test here passes `platform_name="posix"` to exercise the POSIX branch,
# and that branch names `signal.SIGKILL` while building its call — before the
# patched `_kill_process_group` is ever reached. Windows has no such signal, so
# on 2026-08-30 all three failed the Windows job with `module 'signal' has no
# attribute 'SIGKILL'` while passing everywhere else. The condition is the real
# dependency rather than the platform name: a host without the signal cannot
# reach the situation these tests describe.
pytestmark = pytest.mark.skipif(
    not hasattr(signal, "SIGKILL"),
    reason="the POSIX hard-kill path needs SIGKILL, which this host does not have",
)


class _LiveProcess:
    """The only surface `_hard_kill_group` touches."""

    pid = 4242
    joined: list[float]

    def __init__(self) -> None:
        self.joined = []

    def is_alive(self) -> bool:
        return True

    def join(self, timeout: float) -> None:
        self.joined.append(timeout)


def _raising(error: BaseException):
    def kill(pid: int, sig: int) -> None:
        raise error

    return kill


def test_an_already_dead_group_keeps_the_tree_verified(monkeypatch) -> None:
    """SIGTERM won the race; the SIGKILL finding nothing is the good outcome."""
    monkeypatch.setattr(
        memory_queue, "_kill_process_group", _raising(ProcessLookupError())
    )
    process = _LiveProcess()

    assert memory_queue._hard_kill_group(process, "posix", True) is True
    assert process.joined == [0.2]


def test_a_refused_signal_still_fails_closed(monkeypatch) -> None:
    """EPERM says the group may still be there, and that is not verified."""
    monkeypatch.setattr(
        memory_queue, "_kill_process_group", _raising(PermissionError())
    )

    assert memory_queue._hard_kill_group(_LiveProcess(), "posix", True) is False


def test_a_delivered_signal_keeps_the_tree_verified(monkeypatch) -> None:
    monkeypatch.setattr(memory_queue, "_kill_process_group", lambda pid, sig: None)

    assert memory_queue._hard_kill_group(_LiveProcess(), "posix", True) is True


@pytest.mark.parametrize(
    ("platform_name", "tree_verified"),
    (("nt", True), ("posix", False)),
)
def test_the_branch_is_skipped_where_it_does_not_apply(
    monkeypatch, platform_name: str, tree_verified: bool
) -> None:
    """Windows owns its tree elsewhere, and an unverified tree stays unverified."""
    monkeypatch.setattr(
        memory_queue, "_kill_process_group", _raising(AssertionError("called"))
    )

    assert (
        memory_queue._hard_kill_group(_LiveProcess(), platform_name, tree_verified)
        is tree_verified
    )


def test_a_verified_cleanup_of_a_verified_tree_is_not_a_failure() -> None:
    """The verdict this feeds: both halves true means the worker reports nothing."""
    assert memory_queue._cleanup_failed(True, "posix", True) is False
    assert memory_queue._cleanup_failed(True, "posix", False) is True
