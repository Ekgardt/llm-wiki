"""Four places where missing or stale evidence was treated as a settled answer.

See `docs/research/2026-09-17-lsp-what-could-not-be-read-is-not-an-empty-group.md`.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import lsp_process
import lsp_process_tree
import pytest
from lsp_process import GenerationLaunch, LspProcess

from tests.slow_machine import SHORT_TIMEOUT

OWNER_NONCE = "e" * 32


class _Entry:
    """A /proc entry, named like a process, whose `stat` cannot be read."""

    def __init__(self, name: str, path: str) -> None:
        self.name = name
        self.path = path


def test_a_proc_entry_that_cannot_be_read_leaves_the_group_unknown(
    tmp_path: Path,
) -> None:
    unreadable = tmp_path / "4242"
    unreadable.mkdir()
    (unreadable / "stat").mkdir()  # a directory where the file belongs: EISDIR
    entries = [_Entry("4242", str(unreadable))]

    verdict = lsp_process_tree._scan_proc_entries(entries, 4242)

    assert verdict is None


def test_an_entry_proven_outside_the_group_is_passed_over(tmp_path: Path) -> None:
    gone = tmp_path / "4243"
    gone.mkdir()
    entries = [_Entry("4243", str(gone))]

    verdict = lsp_process_tree._scan_proc_entries(entries, 4243)

    assert verdict is True


@pytest.mark.parametrize(
    "name",
    [
        "/proc/self/fd/../../../tmp/evil/7",
        "/dev/fd/../7",
        "/proc/self/fd/7/../7",
        "/proc/self/fd/٧",
    ],
)
def test_only_a_plain_descriptor_number_is_an_inherited_descriptor(name: str) -> None:
    accepted = lsp_process._is_inherited_descriptor(name, (7,))
    assert (accepted, lsp_process._is_inherited_descriptor("/proc/self/fd/7", (7,))) == (
        False,
        True,
    )


@pytest.mark.skipif(os.name != "posix", reason="descriptor paths are a POSIX shape")
def test_a_launch_may_not_name_a_file_behind_a_descriptor_path(tmp_path: Path) -> None:
    elsewhere = tmp_path / "evil"
    elsewhere.mkdir()
    planted = elsewhere / "7"
    planted.write_bytes(b"#!/bin/sh\nexit 0\n")
    planted.chmod(0o700)
    traversal = "/proc/self/fd/" + "../" * 8 + str(planted).lstrip("/")
    launch = GenerationLaunch((traversal, "--stdio"), pass_fds=(7,))

    with pytest.raises(ValueError, match="replace the configured executable"):
        lsp_process._generation_launch(
            ("server",), launch, cwd=tmp_path, owner_root=None
        )


def test_the_recovery_beat_doubles_while_the_same_work_stays_owing() -> None:
    state = lsp_process._RecoveryState(terminal_retry_code="process_exited")
    beats = [state.retry_seconds]
    for _ in range(8):
        state.slow_down()
        beats.append(state.retry_seconds)
    state.quicken()

    assert (beats[1], max(beats), state.retry_seconds) == (
        2 * lsp_process._RECOVERY_RETRY_SECONDS,
        lsp_process._RECOVERY_RETRY_CEILING_SECONDS,
        lsp_process._RECOVERY_RETRY_SECONDS,
    )


@pytest.mark.skipif(os.name != "posix", reason="the fixture server is a POSIX one")
def test_the_channel_a_request_uses_is_taken_under_the_lifecycle_lock(
    tmp_path: Path,
) -> None:
    server = "import sys\nsys.stdin.buffer.read()\n"
    process = LspProcess.start(
        [sys.executable, "-c", server], cwd=tmp_path, owner_root=tmp_path / OWNER_NONCE
    )
    channel = lsp_process._request_generation(process, time.monotonic() + SHORT_TIMEOUT)
    try:
        # What a cleanup running on another thread does to the generation it
        # is releasing, landing the instant after the lock was given back.
        channel.generation.protocol = None
        channel.generation.process = None
        held = (channel.protocol is not None, channel.process is not None)
    finally:
        # Put them back: this test is not the cleanup, and the close that
        # follows has to be able to do its own work.
        channel.generation.protocol = channel.protocol
        channel.generation.process = channel.process
        process.close(time.monotonic() + SHORT_TIMEOUT)
    assert held == (True, True)
