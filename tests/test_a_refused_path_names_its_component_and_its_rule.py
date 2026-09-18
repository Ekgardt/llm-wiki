"""A refused path says which component broke which rule, and a failed build leaves nothing.

See `docs/research/2026-09-18-lsp-a-refusal-names-its-component-and-its-rule.md`.
"""

from __future__ import annotations

from pathlib import Path

import lsp_launch_package
import lsp_security
import pytest
from lsp_security import PathContainmentError


@pytest.mark.parametrize(
    "name, rule",
    [
        ("aux.py", "reserved Windows device name"),
        ("notes:draft.md", "contains one of"),
        ("trailing.", "ends with a dot or a space"),
        ("space ", "ends with a dot or a space"),
    ],
)
def test_the_refusal_names_the_component_and_the_rule(name: str, rule: str) -> None:
    """These are real files on Linux and macOS; a refusal has to say why."""
    with pytest.raises(PathContainmentError) as refused:
        lsp_security._validate_relative_path(f"src/{name}")

    assert (name in str(refused.value), rule in str(refused.value)) == (True, True)


def test_an_oversized_component_is_still_refused_everywhere() -> None:
    with pytest.raises(PathContainmentError, match="longer than"):
        lsp_security._validate_relative_path(
            "src/" + "x" * (lsp_security._MAX_COMPONENT_CHARACTERS + 1)
        )


def test_a_launch_tree_that_cannot_be_built_leaves_nothing_behind(
    tmp_path: Path,
) -> None:
    """The root is made before the nested directory that fails; it must not stay."""
    root = tmp_path / f"{lsp_launch_package.LAUNCH_PREFIX}fixed"
    unmakeable = Path("x" * 5000) / "server.js"

    with pytest.raises(OSError):
        lsp_launch_package._created_directories(root, unmakeable)

    assert not root.exists()
