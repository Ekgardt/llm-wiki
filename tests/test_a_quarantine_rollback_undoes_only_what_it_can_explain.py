from __future__ import annotations

import errno
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from llm_wiki.markdown_records import TargetBoundaryFailure, TransactionFailure
from markdown_transaction import MarkdownChange, MarkdownCoordinator

_NOTE = "knowledge/notes/page.md"


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
def applied(coordinator: MarkdownCoordinator) -> str:
    record = coordinator.prepare(
        [MarkdownChange.replace(_NOTE, b"after\n")], operation_id="applied-replace"
    )
    coordinator.apply(record.id)
    return record.id


@pytest.fixture
def locked_notes(vault: Path) -> Iterator[Path]:
    notes = vault / "knowledge/notes"
    notes.chmod(0o500)
    yield notes
    notes.chmod(0o700)


def _rollback(coordinator: MarkdownCoordinator, transaction_id: str) -> None:
    with coordinator.writer_gate():
        coordinator._rollback_for_quarantine(transaction_id, "precondition_failed")


def _settled(coordinator: MarkdownCoordinator, transaction_id: str) -> tuple[str, object, int]:
    record = coordinator._record(transaction_id)
    applied = coordinator._operation_rows(transaction_id)[0]["applied"]
    return (record.state, record.error_code, applied)


def _replace_notes_directory(vault: Path) -> None:
    notes = vault / "knowledge/notes"
    notes.rename(vault / "knowledge/notes-moved")
    notes.mkdir()
    (notes / "page.md").write_bytes(b"after\n")


def test_an_applied_target_is_restored_before_quarantine(
    coordinator: MarkdownCoordinator, vault: Path, applied: str
) -> None:
    _rollback(coordinator, applied)

    assert _settled(coordinator, applied) == ("quarantined", "precondition_failed", 0)
    assert (vault / _NOTE).read_bytes() == b"before\n"


def test_a_target_changed_by_someone_else_is_left_as_it_stands(
    coordinator: MarkdownCoordinator, vault: Path, applied: str
) -> None:
    (vault / _NOTE).write_bytes(b"third party\n")

    _rollback(coordinator, applied)

    assert _settled(coordinator, applied) == ("quarantined", "precondition_failed", 1)
    assert (vault / _NOTE).read_bytes() == b"third party\n"


def test_a_target_without_its_before_image_is_left_applied(
    coordinator: MarkdownCoordinator, vault: Path, applied: str
) -> None:
    (coordinator.transaction_root / applied / "before/000000.bin").unlink()

    _rollback(coordinator, applied)

    assert _settled(coordinator, applied) == ("quarantined", "precondition_failed", 1)
    assert (vault / _NOTE).read_bytes() == b"after\n"


@pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="needs POSIX permissions that bind the caller; root ignores them",
)
def test_a_target_that_cannot_be_written_back_is_left_applied(
    coordinator: MarkdownCoordinator, vault: Path, applied: str, locked_notes: Path
) -> None:
    _rollback(coordinator, applied)

    assert _settled(coordinator, applied) == ("quarantined", "precondition_failed", 1)
    assert (locked_notes / "page.md").read_bytes() == b"after\n"


def test_a_replaced_parent_quarantines_the_rollback_as_a_boundary_change(
    coordinator: MarkdownCoordinator, vault: Path, applied: str
) -> None:
    _replace_notes_directory(vault)

    _rollback(coordinator, applied)

    assert _settled(coordinator, applied) == ("quarantined", "parent_identity_changed", 1)
    assert (vault / _NOTE).read_bytes() == b"after\n"


def test_a_parent_that_moves_during_the_undo_quarantines_the_rollback(
    coordinator: MarkdownCoordinator,
    vault: Path,
    applied: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def moved_parent(inverse: object, before_state: object) -> None:
        raise TargetBoundaryFailure(f"parent identity mismatch for {_NOTE}")

    monkeypatch.setattr(coordinator, "_apply_inverse_under_fence", moved_parent)

    _rollback(coordinator, applied)

    assert _settled(coordinator, applied) == ("quarantined", "parent_identity_changed", 1)


def test_a_target_error_that_is_no_boundary_change_stops_the_rollback(
    coordinator: MarkdownCoordinator, applied: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    denied = OSError(errno.EACCES, "permission denied")

    def unreadable(row: object) -> str:
        raise denied

    monkeypatch.setattr(coordinator, "_operation_hash", unreadable)

    with pytest.raises(OSError) as raised:
        _rollback(coordinator, applied)

    assert raised.value is denied
    assert _settled(coordinator, applied) == ("committed", None, 1)


def test_a_corrupt_after_image_under_a_replaced_parent_is_quarantined(
    coordinator: MarkdownCoordinator, vault: Path, applied: str
) -> None:
    _replace_notes_directory(vault)

    with coordinator.writer_gate():
        record = coordinator._recover_corrupt_after_image(applied)

    assert (record.state, record.error_code) == ("quarantined", "parent_identity_changed")


def test_a_model_output_publication_under_an_unusable_policy_is_quarantined_by_recovery(
    coordinator: MarkdownCoordinator, vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = coordinator.prepare(
        [MarkdownChange.replace(_NOTE, b"model output\n")],
        operation_id="guarded-replace",
        content_guard="model_output",
    )
    monkeypatch.setenv("LLM_WIKI_DLP_POLICY", "relative-policy.json")

    recovered = coordinator.recover()

    assert [(item.id, item.state, item.error_code) for item in recovered] == [
        (record.id, "quarantined", "dlp_policy_error")
    ]
    assert (vault / _NOTE).read_bytes() == b"before\n"


def test_an_apply_under_an_unusable_policy_names_the_policy(
    coordinator: MarkdownCoordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = coordinator.prepare(
        [MarkdownChange.replace(_NOTE, b"model output\n")],
        operation_id="guarded-apply",
        content_guard="model_output",
    )
    monkeypatch.setenv("LLM_WIKI_DLP_POLICY", "relative-policy.json")

    with pytest.raises(TransactionFailure) as raised:
        coordinator.apply(record.id)

    assert (raised.value.code, raised.value.state) == ("dlp_policy_error", "quarantined")
