"""Two installers starting together must wait, not abort.

`windows_workspace.create_file` reports the raw Windows error code. When another
process holds the pyright install lock, or has just marked it for deletion, that
code is ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION or ERROR_LOCK_VIOLATION —
which mean exactly what FileExistsError means here. Before this, only
FileExistsError was read as a lost race and the rest aborted the install with
`pyright_install_io_failed`, which is what timing::windows_full reported.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import install_pyright  # noqa: E402

CONTENTION_CODES = (5, 32, 33)


@pytest.fixture()
def on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "name", "nt")


@pytest.mark.parametrize("code", CONTENTION_CODES)
def test_a_contended_lock_is_a_lost_race(
    monkeypatch: pytest.MonkeyPatch, on_windows: None, code: int
) -> None:
    def refuse(_parent: object, _name: str) -> object:
        raise OSError(code, "cannot create Windows component: .install-pyright-lock")

    monkeypatch.setattr(install_pyright, "_create_child_file", refuse)

    assert install_pyright._created_lock_file(object()) is None


def test_an_existing_lock_is_still_a_lost_race(
    monkeypatch: pytest.MonkeyPatch, on_windows: None
) -> None:
    def refuse(_parent: object, _name: str) -> object:
        raise FileExistsError(17, "file exists")

    monkeypatch.setattr(install_pyright, "_create_child_file", refuse)

    assert install_pyright._created_lock_file(object()) is None


def test_a_real_failure_still_surfaces(
    monkeypatch: pytest.MonkeyPatch, on_windows: None
) -> None:
    """Only the contention codes are read as a race; the rest are still errors."""

    def refuse(_parent: object, _name: str) -> object:
        raise OSError(112, "there is not enough space on the disk")

    monkeypatch.setattr(install_pyright, "_create_child_file", refuse)

    with pytest.raises(OSError):
        install_pyright._created_lock_file(object())


@pytest.mark.parametrize("code", CONTENTION_CODES)
def test_posix_reads_none_of_these_as_contention(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    """These are Windows codes; on POSIX the same numbers mean other things."""
    monkeypatch.setattr(os, "name", "posix")

    def refuse(_parent: object, _name: str) -> object:
        raise OSError(code, "an ordinary POSIX failure")

    monkeypatch.setattr(install_pyright, "_create_child_file", refuse)

    with pytest.raises(OSError):
        install_pyright._created_lock_file(object())
