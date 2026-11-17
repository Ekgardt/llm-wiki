from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from llm_wiki import markdown_store, markdown_targets
from llm_wiki.markdown_records import (
    PreconditionChangedError,
    TargetBoundaryFailure,
)
from markdown_transaction import MarkdownChange, MarkdownCoordinator

_NOTE = "knowledge/notes/page.md"


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "knowledge/notes").mkdir(parents=True)
    return root


@pytest.fixture
def coordinator(vault: Path, tmp_path: Path) -> MarkdownCoordinator:
    return MarkdownCoordinator(vault, tmp_path / "state")


@pytest.fixture
def page(vault: Path) -> Path:
    target = vault / _NOTE
    target.write_bytes(b"12345678")
    return target


def _appending_before(
    target: Path, extra: bytes, call: Callable[[int, Path], tuple[str, int]]
) -> Callable[[int, Path], tuple[str, int]]:
    def append_then_call(descriptor: int, label: Path) -> tuple[str, int]:
        with target.open("ab") as handle:
            handle.write(extra)
        return call(descriptor, label)

    return append_then_call


def _first_answer_from(
    stand_in: Path, call: Callable[[Path], os.stat_result | None]
) -> Callable[[Path], os.stat_result | None]:
    answers = [os.lstat(stand_in)]

    def answer(target: Path) -> os.stat_result | None:
        if answers:
            return answers.pop()
        return call(target)

    return answer


def test_a_target_that_is_a_link_is_not_read(coordinator: MarkdownCoordinator, page: Path) -> None:
    link = page.with_name("link.md")
    link.symlink_to(page)

    with pytest.raises(ValueError, match="transaction target is a link"):
        coordinator._read_target(link)


def test_a_target_that_is_a_directory_is_not_read(
    coordinator: MarkdownCoordinator, vault: Path
) -> None:
    directory = vault / "knowledge/notes/folder.md"
    directory.mkdir()

    with pytest.raises(ValueError, match="transaction target is not a regular file"):
        coordinator._read_target(directory)


def test_an_unopenable_target_is_named_as_unsafe(vault: Path) -> None:
    missing = vault / "knowledge/notes/missing.md"

    with pytest.raises(ValueError, match="transaction target cannot be opened safely"):
        markdown_targets._open_capture_descriptor(missing, missing)


def test_an_unbounded_read_returns_every_byte(coordinator: MarkdownCoordinator, page: Path) -> None:
    assert coordinator._read_bounded_target(page, None) == b"12345678"


def test_a_read_shorter_than_the_file_size_is_a_change(page: Path) -> None:
    with pytest.raises(PreconditionChangedError, match="changed while reading"):
        markdown_targets._require_read_size(b"1234", os.stat(page), page, None)


def test_a_read_longer_than_its_bound_is_refused(page: Path) -> None:
    with pytest.raises(ValueError, match="exceeds 4 bytes"):
        markdown_targets._require_read_size(b"12345678", os.stat(page), page, 4)


def test_a_target_swapped_before_it_was_opened_is_a_change(
    coordinator: MarkdownCoordinator,
    page: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stand_in = tmp_path / "stand-in.md"
    stand_in.write_bytes(b"87654321")
    monkeypatch.setattr(
        markdown_targets,
        "lstat_or_none",
        _first_answer_from(stand_in, markdown_store.lstat_or_none),
    )

    with pytest.raises(PreconditionChangedError, match="changed before open"):
        coordinator._read_target(page)


def test_a_target_written_while_it_is_read_is_a_change(
    coordinator: MarkdownCoordinator, page: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_read = markdown_targets.descriptor_bytes

    def read_while_appending(descriptor: int, max_bytes: int | None) -> bytes:
        with page.open("ab") as handle:
            handle.write(b"9")
        return real_read(descriptor, max_bytes)

    monkeypatch.setattr(markdown_targets, "descriptor_bytes", read_while_appending)

    with pytest.raises(PreconditionChangedError, match="changed while reading"):
        coordinator._read_target(page)


def test_a_target_past_the_bound_hashes_as_oversized_without_being_read(
    coordinator: MarkdownCoordinator, page: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(markdown_store, "MAX_KNOWLEDGE_TARGET_BYTES", 4)

    assert coordinator._hash_operation_target(page, None) == "oversized"


@contextlib.contextmanager
def _parent_descriptor(page: Path, through_parent: bool) -> Iterator[int | None]:
    if not through_parent:
        yield None
        return
    descriptor = os.open(page.parent, os.O_RDONLY)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("through_parent", [False, True])
def test_a_target_that_grows_past_the_bound_while_hashed_is_oversized(
    coordinator: MarkdownCoordinator,
    page: Path,
    monkeypatch: pytest.MonkeyPatch,
    through_parent: bool,
) -> None:
    monkeypatch.setattr(markdown_store, "MAX_KNOWLEDGE_TARGET_BYTES", 8)
    monkeypatch.setattr(
        markdown_targets,
        "_bounded_digest",
        _appending_before(page, b"grown", markdown_targets._bounded_digest),
    )

    with _parent_descriptor(page, through_parent) as descriptor:
        assert coordinator._hash_operation_target(page, descriptor) == "oversized"


def test_a_target_written_while_it_is_hashed_is_a_change(
    coordinator: MarkdownCoordinator, page: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        markdown_targets,
        "_bounded_digest",
        _appending_before(page, b"9", markdown_targets._bounded_digest),
    )

    with pytest.raises(PreconditionChangedError, match="changed while hashing"):
        coordinator._hash_operation_target(page, None)


def test_a_target_swapped_before_it_was_hashed_is_a_change(
    coordinator: MarkdownCoordinator,
    page: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stand_in = tmp_path / "stand-in.md"
    stand_in.write_bytes(b"87654321")
    monkeypatch.setattr(
        markdown_targets,
        "lstat_or_none",
        _first_answer_from(stand_in, markdown_store.lstat_or_none),
    )

    with pytest.raises(PreconditionChangedError, match="changed before hash"):
        coordinator._hash_operation_target(page, None)


def test_a_vanished_parent_is_a_boundary_failure(
    coordinator: MarkdownCoordinator, vault: Path
) -> None:
    gone = vault / "knowledge/notes/gone"

    with pytest.raises(TargetBoundaryFailure, match="not a stable directory"):
        coordinator._parent_identity(gone)
    with (
        pytest.raises(TargetBoundaryFailure, match="not a stable directory"),
        coordinator._capture_target(gone / "page.md"),
    ):
        pass


def test_a_parent_error_that_is_not_about_the_boundary_is_raised_as_it_is(
    vault: Path,
) -> None:
    denied = OSError(errno.EACCES, "permission denied")

    with pytest.raises(OSError) as raised:
        markdown_targets._as_parent_boundary_failure(vault, denied)

    assert raised.value is denied


def _without_windows_handles(
    coordinator: MarkdownCoordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(markdown_targets, "use_posix_dir_fd", lambda: False)
    monkeypatch.setattr(
        coordinator, "_hold_windows_parent", lambda parent: contextlib.nullcontext()
    )


def test_the_path_route_captures_the_target_and_its_parent_identity(
    coordinator: MarkdownCoordinator, page: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_windows_handles(coordinator, monkeypatch)

    with coordinator._capture_target(page, max_before_bytes=None) as (source, identity):
        captured = b"".join(source.iter_chunks(0, len(source)))

    assert (captured, identity) == (
        b"12345678",
        markdown_targets._stat_identity(os.stat(page.parent)),
    )


def test_the_path_route_captures_an_absent_target_as_none(
    coordinator: MarkdownCoordinator, vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_windows_handles(coordinator, monkeypatch)

    with coordinator._capture_target(vault / "knowledge/notes/new.md") as (source, _):
        assert source is None


def _prepared_operation(coordinator: MarkdownCoordinator) -> sqlite3.Row:
    record = coordinator.prepare(
        [MarkdownChange.replace(_NOTE, b"after")], operation_id="stable-parent"
    )
    row: sqlite3.Row = coordinator._operation_rows(record.id)[0]
    return row


def test_the_path_route_hashes_a_target_under_its_recorded_parent(
    coordinator: MarkdownCoordinator, page: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _prepared_operation(coordinator)
    _without_windows_handles(coordinator, monkeypatch)

    with coordinator._stable_parent(row) as (target, descriptor):
        current = coordinator._hash_operation_target(target, descriptor)

    assert (target, descriptor, current) == (
        page,
        None,
        hashlib.sha256(b"12345678").hexdigest(),
    )


def _with_parent_inode(row: sqlite3.Row, inode: int) -> sqlite3.Row:
    database = sqlite3.connect(":memory:")
    database.row_factory = sqlite3.Row
    values = dict(zip(row.keys(), row, strict=True)) | {"parent_inode": inode}
    columns = ", ".join(f"? AS {name}" for name in values)
    changed = database.execute(f"SELECT {columns}", tuple(values.values())).fetchone()
    database.close()
    assert isinstance(changed, sqlite3.Row)
    return changed


def test_a_parent_replaced_since_prepare_is_refused(
    coordinator: MarkdownCoordinator, page: Path
) -> None:
    row = _prepared_operation(coordinator)
    moved = _with_parent_inode(row, row["parent_inode"] + 1)

    with (
        pytest.raises(TargetBoundaryFailure, match=f"parent identity mismatch for {_NOTE}"),
        coordinator._stable_parent(moved),
    ):
        pass
    assert page.read_bytes() == b"12345678"
