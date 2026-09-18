"""The private index arms on the interpreter that runs it, and on a cloned checkout.

Third audit, G-M8 point 3. Research:
`docs/research/2026-09-18-a-cloned-checkout-keeps-the-private-read.md`.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
for directory in (TESTS.parent / "scripts", TESTS):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

CLONE_CONFIG = b"""[core]
\trepositoryformatversion = 0
\tfilemode = true
\tbare = false
\tlogallrefupdates = true
[remote "origin"]
\turl = https://example.invalid/project.git
\tfetch = +refs/heads/*:refs/remotes/origin/*
[branch "main"]
\tremote = origin
\tmerge = refs/heads/main
\tvscode-merge-base = origin/main
"""


class _FcntlWithoutSeals:
    """An `fcntl` module built without the Linux seal constants, as uv's CPython is."""

    def __init__(self, real) -> None:
        self._real = real

    def fcntl(self, *arguments):
        return self._real.fcntl(*arguments)


def test_a_config_a_clone_writes_keeps_the_private_read(tmp_path):
    import workspace_revision

    accepted = workspace_revision._safe_private_git_config(
        CLONE_CONFIG, hash_name="sha1"
    )

    assert accepted is True


@pytest.mark.parametrize(
    "setting", ["promisor = true", "partialclonefilter = blob:none"]
)
def test_a_remote_that_can_fetch_during_a_read_still_refuses_it(setting):
    import workspace_revision

    config = CLONE_CONFIG.replace(
        b"\tfetch = +refs/heads/*:refs/remotes/origin/*\n",
        b"\tfetch = +refs/heads/*:refs/remotes/origin/*\n\t" + setting.encode() + b"\n",
    )

    assert workspace_revision._safe_private_git_config(config, hash_name="sha1") is False


def test_a_section_that_can_run_a_command_still_refuses_the_private_read():
    import workspace_revision

    config = CLONE_CONFIG + b'[filter "lfs"]\n\tclean = git-lfs clean -- %f\n'

    assert workspace_revision._safe_private_git_config(config, hash_name="sha1") is False


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="file seals are a Linux ABI"
)
def test_an_interpreter_without_the_seal_constants_still_seals(monkeypatch):
    """The constants are the kernel's, not the build's; the seals are read back."""
    import fcntl

    import workspace_revision

    monkeypatch.setattr(workspace_revision, "_fcntl", _FcntlWithoutSeals(fcntl))
    descriptor = os.memfd_create("llm-wiki-test", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)

    try:
        available = workspace_revision._private_index_runtime_available()
        sealed = workspace_revision._seal_private_index(descriptor)
    finally:
        os.close(descriptor)

    assert (available, sealed) == (True, True)
