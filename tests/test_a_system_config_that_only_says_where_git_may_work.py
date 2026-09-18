"""A `[safe]` section in a system git config does not turn the private read off.

Every GitHub-hosted Linux runner ships an `/etc/gitconfig` holding
`[safe]` / `directory = *` — `actions/runner-images` appends it in
`install-git.sh`. `safe.directory` says only whether git agrees to work in a
repository at all, never what it reads of the working tree, yet the semantics
fence refused the section outright, the private-index fast path was dead on
every one of those machines, and 67 tests said so in run 35363057747 the moment
`fc91a047` stopped them skipping. Research:
`docs/research/2026-09-18-a-system-config-that-only-says-where-git-may-work.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
for _directory in (TESTS.parent / "scripts", TESTS):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

import workspace_revision  # noqa: E402

# What `actions/runner-images` appends to /etc/gitconfig, byte for byte.
RUNNER_SYSTEM_CONFIG = b"[safe]\n        directory = *\n"

# What `git lfs install --skip-repo --system` writes there - which the git-lfs
# Debian package's postinst runs, and which the same runner image installs. A
# clean/smudge filter changes what git reads of a working tree, so the fence must
# refuse it and the fast path must stay off on such a machine.
LFS_SYSTEM_CONFIG = (
    b'[filter "lfs"]\n'
    b"\tclean = git-lfs clean -- %f\n"
    b"\tsmudge = git-lfs smudge -- %f\n"
    b"\tprocess = git-lfs filter-process\n"
    b"\trequired = true\n"
)


@pytest.mark.parametrize(
    ("content", "inert"),
    [
        (RUNNER_SYSTEM_CONFIG, True),
        (b"[safe]\n\tbareRepository = explicit\n", True),
        (b"[user]\n\temail = someone@example.invalid\n", True),
        # A key `[safe]` may not carry, and a section that changes what git reads.
        (b"[safe]\n\tdirectory = *\n\tsomethingElse = yes\n", False),
        (b"[core]\n\tautocrlf = false\n", False),
        (b"[safe]\n\tdirectory = *\n[core]\n\tautocrlf = false\n", False),
        # A hosted Linux runner's real file: the filter first, the section after.
        (LFS_SYSTEM_CONFIG, False),
        (LFS_SYSTEM_CONFIG + RUNNER_SYSTEM_CONFIG, False),
    ],
)
def test_only_a_section_that_cannot_change_what_git_reads_is_ignored(
    content: bytes, inert: bool
) -> None:
    """The fence tolerates `[safe]`'s two keys and nothing else it did not before."""
    assert workspace_revision._safe_ignored_git_config(content) is inert


def test_the_runner_system_config_lets_the_private_read_arm(tmp_path, monkeypatch) -> None:
    """The whole point: this file is why the fast path was dead on every runner."""
    config = tmp_path / "system-gitconfig"
    config.write_bytes(RUNNER_SYSTEM_CONFIG)
    monkeypatch.setattr(
        workspace_revision, "_SYSTEM_GIT_CONFIG_PATHS", (config,), raising=False
    )

    assert workspace_revision._safe_ignored_git_config(config.read_bytes()) is True
