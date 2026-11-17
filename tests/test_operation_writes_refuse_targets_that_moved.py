from __future__ import annotations

import contextlib
import ctypes
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from llm_wiki import markdown_operations, markdown_targets
from llm_wiki.markdown_records import (
    TargetBoundaryFailure,
    TargetStateMismatch,
    TransactionImageError,
)
from markdown_transaction import MarkdownChange, MarkdownCoordinator

_NOTE = "knowledge/notes/page.md"


class _MutationKernel32:
    def __init__(self, handle: object) -> None:
        self.handle = handle

    def CreateFileW(self, *arguments: object) -> object:
        return self.handle


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "knowledge/notes").mkdir(parents=True)
    (root / _NOTE).write_bytes(b"before\n")
    return root


@pytest.fixture
def coordinator(vault: Path, tmp_path: Path) -> MarkdownCoordinator:
    return MarkdownCoordinator(vault, tmp_path / "state")


@pytest.fixture
def directory(tmp_path: Path) -> Iterator[tuple[Path, int]]:
    folder = tmp_path / "folder"
    folder.mkdir()
    descriptor = os.open(folder, os.O_RDONLY)
    yield folder, descriptor
    os.close(descriptor)


def _with(row: sqlite3.Row, **changes: object) -> sqlite3.Row:
    database = sqlite3.connect(":memory:")
    database.row_factory = sqlite3.Row
    values = dict(zip(row.keys(), row, strict=True)) | changes
    columns = ", ".join(f"? AS {name}" for name in values)
    changed = database.execute(f"SELECT {columns}", tuple(values.values())).fetchone()
    database.close()
    assert isinstance(changed, sqlite3.Row)
    return changed


def _prepared(coordinator: MarkdownCoordinator, change: MarkdownChange) -> sqlite3.Row:
    record = coordinator.prepare([change], operation_id=f"{change.kind}-operation")
    row: sqlite3.Row = coordinator._operation_rows(record.id)[0]
    return row


def _after_plan(coordinator: MarkdownCoordinator, row: sqlite3.Row) -> dict[str, object]:
    plan = coordinator._load_verified_plan(coordinator._record(row["transaction_id"]))
    operations = plan["operations"]
    assert isinstance(operations, list)
    entry: dict[str, object] = operations[0]
    return entry


def test_a_create_over_a_file_that_appeared_is_a_before_mismatch(
    directory: tuple[Path, int],
) -> None:
    folder, descriptor = directory
    (folder / "staged.tmp").write_bytes(b"new")
    (folder / "page.md").write_bytes(b"someone else")

    with pytest.raises(TargetStateMismatch, match="before state mismatch for page"):
        markdown_operations._link_or_replace("create", "staged.tmp", "page.md", descriptor, "page")

    assert (folder / "page.md").read_bytes() == b"someone else"


def test_a_delete_of_a_file_that_vanished_is_a_before_mismatch(
    coordinator: MarkdownCoordinator, vault: Path, directory: tuple[Path, int]
) -> None:
    row = _prepared(coordinator, MarkdownChange.delete(_NOTE))
    _folder, descriptor = directory

    with pytest.raises(TargetStateMismatch, match=f"before state mismatch for {_NOTE}"):
        coordinator._unlink_at_parent(row, vault / "absent.md", descriptor)


def test_a_file_created_in_a_parent_that_vanished_is_a_boundary_failure(
    directory: tuple[Path, int],
) -> None:
    folder, descriptor = directory
    folder.rmdir()

    with pytest.raises(TargetBoundaryFailure, match="parent identity is not a stable directory"):
        markdown_operations.OperationExecution._create_at_parent(descriptor, "page.md")


def test_an_applied_operation_whose_target_changed_is_an_after_mismatch(
    coordinator: MarkdownCoordinator, vault: Path
) -> None:
    row = _prepared(coordinator, MarkdownChange.replace(_NOTE, b"after\n"))
    coordinator.apply(row["transaction_id"])
    (vault / _NOTE).write_bytes(b"third party\n")
    applied = coordinator._operation_rows(row["transaction_id"])[0]

    with pytest.raises(TargetStateMismatch, match=f"after state mismatch for {_NOTE}"):
        coordinator._reconcile_one_operation(row["transaction_id"], applied)


def test_a_target_that_no_longer_holds_its_before_state_is_refused(
    coordinator: MarkdownCoordinator, vault: Path
) -> None:
    row = _prepared(coordinator, MarkdownChange.replace(_NOTE, b"after\n"))
    (vault / _NOTE).write_bytes(b"third party\n")

    with pytest.raises(TargetStateMismatch, match=f"before state mismatch for {_NOTE}"):
        coordinator._require_operation_target_state(vault / _NOTE, None, row, "before")


def test_an_operation_plan_without_an_after_image_is_refused(
    coordinator: MarkdownCoordinator,
) -> None:
    row = _prepared(coordinator, MarkdownChange.replace(_NOTE, b"after\n"))

    with pytest.raises(RuntimeError, match="transaction after-image is absent"):
        coordinator._verified_after_content(row, {"after": "absent"})


def test_an_after_image_that_does_not_hash_to_the_row_is_corrupt(
    coordinator: MarkdownCoordinator,
) -> None:
    row = _prepared(coordinator, MarkdownChange.replace(_NOTE, b"after\n"))
    entry = _after_plan(coordinator, row)

    with pytest.raises(TransactionImageError, match=f"after-image is corrupt for {_NOTE}"):
        coordinator._verified_after_content(_with(row, after_hash="0" * 64), entry)


def test_the_bytes_of_a_target_that_vanished_are_a_before_mismatch(
    coordinator: MarkdownCoordinator,
) -> None:
    row = _prepared(coordinator, MarkdownChange.replace(_NOTE, b"after\n"))

    with pytest.raises(TargetStateMismatch, match="before state mismatch"):
        coordinator._current_target_bytes(_with(row, path="knowledge/notes/absent.md"))


def test_a_plan_without_a_described_before_state_authenticates_no_prefix(
    coordinator: MarkdownCoordinator,
) -> None:
    row = _prepared(coordinator, MarkdownChange.replace(_NOTE, b"after\n"))

    assert coordinator._authenticated_publication_prefix(row, {"before": "absent"}) is None


def test_a_windows_mutation_handle_that_opens_is_returned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel32 = _MutationKernel32(42)
    monkeypatch.setattr(markdown_operations, "windows_kernel32", lambda: kernel32)

    assert markdown_operations.open_windows_file_for_mutation(tmp_path / "page.md") == 42


@contextlib.contextmanager
def _held_windows_parent(coordinator: MarkdownCoordinator, parent: Path) -> Iterator[None]:
    coordinator._local.windows_parent_handle = 7
    try:
        yield
    finally:
        coordinator._local.windows_parent_handle = None


def _on_the_windows_route(
    coordinator: MarkdownCoordinator, monkeypatch: pytest.MonkeyPatch, held: bool
) -> None:
    monkeypatch.setattr(markdown_targets, "use_posix_dir_fd", lambda: False)
    monkeypatch.setattr(coordinator, "_hold_windows_parent", contextlib.nullcontext)
    if held:
        monkeypatch.setattr(
            coordinator,
            "_hold_windows_parent",
            lambda parent: _held_windows_parent(coordinator, parent),
        )


def test_the_windows_route_refuses_to_mutate_without_a_held_parent(
    coordinator: MarkdownCoordinator, vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _prepared(coordinator, MarkdownChange.replace(_NOTE, b"after\n"))
    _on_the_windows_route(coordinator, monkeypatch, held=False)

    with pytest.raises(RuntimeError, match="Windows parent handle is not held at mutation"):
        coordinator._apply_operation(row, _after_plan(coordinator, row))

    assert (vault / _NOTE).read_bytes() == b"before\n"


def test_the_windows_route_publishes_a_replacement_under_a_held_parent(
    coordinator: MarkdownCoordinator, vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _prepared(coordinator, MarkdownChange.replace(_NOTE, b"after\n"))
    _on_the_windows_route(coordinator, monkeypatch, held=True)

    coordinator._apply_operation(row, _after_plan(coordinator, row))

    assert (vault / _NOTE).read_bytes() == b"after\n"
    assert sorted(path.name for path in (vault / "knowledge/notes").iterdir()) == ["page.md"]


class _DeletionLedger:
    def __init__(self, target: Path) -> None:
        self.target = target
        self.events: list[str] = []

    def open(self, path: Path) -> int:
        self.events.append(f"open {path.name}")
        return 9

    def delete(self, handle: int) -> None:
        self.events.append(f"delete {handle}")
        self.target.unlink()

    def close(self, handle: int) -> None:
        self.events.append(f"close {handle}")


def test_the_windows_route_deletes_through_a_disposition_handle(
    coordinator: MarkdownCoordinator, vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _prepared(coordinator, MarkdownChange.delete(_NOTE))
    _on_the_windows_route(coordinator, monkeypatch, held=True)
    ledger = _DeletionLedger(vault / _NOTE)
    monkeypatch.setattr(markdown_operations, "open_windows_file_for_mutation", ledger.open)
    monkeypatch.setattr(markdown_operations, "delete_windows_handle", ledger.delete)
    monkeypatch.setattr(markdown_operations, "close_windows_handle", ledger.close)

    coordinator._apply_operation(row, _after_plan(coordinator, row))

    assert ledger.events == ["open page.md", "delete 9", "close 9"]
    assert not (vault / _NOTE).exists()


def test_a_windows_mutation_handle_that_cannot_open_names_the_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel32 = _MutationKernel32(ctypes.c_void_p(-1).value)
    monkeypatch.setattr(markdown_operations, "windows_kernel32", lambda: kernel32)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 32, raising=False)

    with pytest.raises(OSError, match="cannot open Windows mutation handle") as raised:
        markdown_operations.open_windows_file_for_mutation(tmp_path / "page.md")

    assert raised.value.errno == 32
