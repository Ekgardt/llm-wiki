"""A repeated append answers `committed` only while the file it wrote still exists.

The committed record was answered whatever had become of the target, so append,
delete, append said "written" over a file that was not there. Permanent operation
ids make that reachable: the daily header and the vault-log header are asked
again by name every time their file is absent.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import markdown_transaction  # noqa: E402
from markdown_transaction import MarkdownCoordinator  # noqa: E402

from tests.slow_machine import LONG_TIMEOUT  # noqa: E402

_LOG = "knowledge/daily/2026-09-18.md"


def _coordinator(tmp_path: Path) -> MarkdownCoordinator:
    root = tmp_path / "vault"
    (root / "knowledge/daily").mkdir(parents=True)
    return MarkdownCoordinator(root, tmp_path / "state")


def _append(coordinator: MarkdownCoordinator, block: bytes) -> str:
    record = markdown_transaction._append_until_committed(
        coordinator,
        "daily-header:2026-09-18",
        _LOG,
        block,
        cancelled=None,
        deadline=time.monotonic() + LONG_TIMEOUT,
    )
    return record.state


def test_the_same_append_asked_twice_writes_once(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    target = coordinator.vault / _LOG

    first = _append(coordinator, b"# header\n")
    second = _append(coordinator, b"# header\n")

    assert (first, second, target.read_bytes()) == ("committed", "committed", b"# header\n")


def test_an_append_whose_file_was_deleted_is_written_again(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    target = coordinator.vault / _LOG
    _append(coordinator, b"# header\n")
    target.unlink()

    state = _append(coordinator, b"# header\n")

    assert (state, target.read_bytes()) == ("committed", b"# header\n")


def test_other_writers_appending_to_the_file_do_not_make_it_write_again(
    tmp_path: Path,
) -> None:
    """A growing log is the ordinary case, not this block's disappearance."""
    coordinator = _coordinator(tmp_path)
    target = coordinator.vault / _LOG
    _append(coordinator, b"# header\n")
    markdown_transaction._append_until_committed(
        coordinator,
        "another-writer",
        _LOG,
        b"a later line\n",
        cancelled=None,
        deadline=time.monotonic() + LONG_TIMEOUT,
    )

    state = _append(coordinator, b"# header\n")

    assert (state, target.read_bytes()) == ("committed", b"# header\na later line\n")
