"""Directory-construction cleanup remains tested on every platform."""
from pathlib import Path

import lsp_launch_package
import pytest


def test_a_launch_tree_that_cannot_be_built_leaves_nothing_behind(
    tmp_path: Path,
) -> None:
    """The root is made before the nested directory that fails; it must not stay."""
    root = tmp_path / f"{lsp_launch_package.LAUNCH_PREFIX}fixed"
    unmakeable = Path("x" * 5000) / "server.js"

    with pytest.raises(OSError):
        lsp_launch_package.created_directories(root, unmakeable)

    assert not root.exists()
