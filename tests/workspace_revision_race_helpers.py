"""Helpers shared by the private-index race and git-process tests."""


from __future__ import annotations

from pathlib import Path

import pytest
import workspace_revision

from tests.workspace_revision_helpers import (
    _isolated_global_git_configuration,  # noqa: F401
    )


def _qualify_private_git_wrapper(
    monkeypatch: pytest.MonkeyPatch,
    wrapper: Path,
) -> None:
    installation = workspace_revision.private_git_installation()
    assert installation is not None
    executable = wrapper.resolve(strict=True)
    replacement = type(installation)(
        executable,
        workspace_revision.strong_identity(executable.lstat()),
        installation.system_config_paths,
        installation.system_attribute_paths,
    )
    monkeypatch.setattr(workspace_revision, "private_git_installation", lambda: replacement)
