"""A directory link that stays inside the checkout is passed by; one that leaves it is refused.

Every Linux virtual environment holds `lib64 -> lib`, and the walk refused the
whole checkout for it. Research:
`docs/research/2026-09-17-a-directory-link-inside-the-checkout-is-passed-by.md`.

Since `docs/research/2026-09-17-a-revision-walks-what-the-corpus-walks.md` the
walk prunes hidden directories, so `.venv` is no longer reached at all; the rule
these tests pin is the general one, and the directory here is an ordinary
vendored tree. Since 2026-09-25 a top-level directory git ignores whole is not
walked at all (audit A-16): the ignored environment is skipped, and the link that
leaves the checkout is tested in a tracked tree, where the walk still meets it.
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

from test_repository_index import _repository  # noqa: E402

ENVIRONMENT_MODULE = "vendor/lib/python3/site-packages/dep.py"


def _directory_link_or_skip(link: Path, target: Path | str) -> None:
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable")


def _checkout_with_environment(path: Path, *, ignored: bool = False) -> Path:
    files = {"pkg/alpha.py": "def alpha():\n    return 1\n", ENVIRONMENT_MODULE: "X = 1\n"}
    if ignored:
        files[".gitignore"] = "vendor/\n"
    return _repository(path, files)


def _revision(root: Path):
    from repository_scope import resolve_repository_scope
    from workspace_revision import compute_workspace_revision

    return compute_workspace_revision(resolve_repository_scope(root))


def test_a_checkout_with_a_virtual_environment_link_has_a_revision_that_verifies(tmp_path):
    from repository_scope import resolve_repository_scope
    from workspace_revision import verify_workspace_revision_unchanged

    root = _checkout_with_environment(tmp_path / "repo", ignored=True)
    before = _revision(root)
    _directory_link_or_skip(root / "vendor" / "lib64", "lib")

    after = _revision(root)

    paths = [entry.path for entry in after.entries]
    assert (after.revision_sha256, [path for path in paths if path.startswith("vendor/")]) == (before.revision_sha256, [])
    assert verify_workspace_revision_unchanged(resolve_repository_scope(root), after) is True


def test_a_directory_link_that_leaves_the_checkout_is_still_refused(tmp_path):
    root = _checkout_with_environment(tmp_path / "repo")
    outside = tmp_path / "outside"
    outside.mkdir()
    _directory_link_or_skip(root / "vendor" / "lib64", outside)

    with pytest.raises(PermissionError, match="symlink or reparse directory"):
        _revision(root)
