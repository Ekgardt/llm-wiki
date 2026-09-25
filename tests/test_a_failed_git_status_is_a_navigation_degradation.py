"""A Git command the revision runs that exits non-zero is a `ValueError` the navigation degrades on.

It was `subprocess.CalledProcessError`, which the navigation does not catch, so a
failed `git status` reached the caller as a generic error. See
docs/research/2026-09-25-a-failed-git-status-is-a-navigation-degradation.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import workspace_revision


def test_a_failing_git_command_raises_the_degrading_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exit code") as failure:
        workspace_revision._git_output(
            tmp_path,
            ["status", "--porcelain=v2"],
            maximum_bytes=1024,
            label="Git state",
            deadline=None,
            cancelled=None,
        )

    assert isinstance(failure.value, workspace_revision.GitCommandFailed)
