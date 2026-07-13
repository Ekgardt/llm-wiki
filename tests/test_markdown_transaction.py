from __future__ import annotations

import errno
import inspect
import os
import sqlite3
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import markdown_transaction
import pytest
from markdown_transaction import MarkdownChange, MarkdownCoordinator
from reliable_memory import sha256_bytes


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    for relative in (
        "knowledge/daily",
        "knowledge/notes",
        "knowledge/projects",
        "knowledge/projects/demo",
        "knowledge/inbox",
        "knowledge/inbox/claims",
    ):
        (root / relative).mkdir(parents=True)
    (root / "knowledge/index.md").write_bytes(b"index-v1\n")
    (root / "knowledge/log.md").write_bytes(b"log-v1\n")
    return root


@pytest.fixture
def state_root(tmp_path: Path) -> Path:
    return tmp_path / "state"


def test_prepare_and_apply_create_replace_delete(vault: Path, state_root: Path):
    old = vault / "knowledge/inbox/claims/old.md"
    old.write_bytes(b"old\n")
    coordinator = MarkdownCoordinator(vault, state_root)

    transaction = coordinator.prepare(
        [
            MarkdownChange.create("knowledge/notes/new.md", b"new\n"),
            MarkdownChange.replace("knowledge/index.md", b"index-v2\n"),
            MarkdownChange.delete("knowledge/inbox/claims/old.md"),
            MarkdownChange.replace("knowledge/log.md", b"log-v2\n"),
        ],
        operation_id="compile:abc",
    )

    assert transaction.state == "prepared"
    assert transaction.operations[0].before_hash == "absent"
    assert transaction.operations[0].after_hash == sha256_bytes(b"new\n")
    assert transaction.operations[2].before_hash == sha256_bytes(b"old\n")
    assert transaction.operations[2].after_hash == "absent"
    assert not (vault / "knowledge/notes/new.md").exists()

    committed = coordinator.apply(transaction.id)
    assert committed.state == "committed"
    assert (vault / "knowledge/notes/new.md").read_bytes() == b"new\n"
    assert (vault / "knowledge/index.md").read_bytes() == b"index-v2\n"
    assert (vault / "knowledge/log.md").read_bytes() == b"log-v2\n"
    assert not old.exists()


def test_prepare_requires_keyword_only_operation_id():
    parameter = inspect.signature(MarkdownCoordinator.prepare).parameters["operation_id"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


def test_prepare_captures_before_images_and_fsyncs_every_artifact(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    target = vault / "knowledge/notes/page.md"
    target.write_bytes(b"before\n")
    synced: list[Path] = []
    real_fsync = markdown_transaction.fsync_file

    def recording_fsync(path: Path):
        synced.append(Path(path))
        real_fsync(path)

    monkeypatch.setattr(markdown_transaction, "fsync_file", recording_fsync)
    coordinator = MarkdownCoordinator(vault, state_root)
    transaction = coordinator.prepare(
        [MarkdownChange.replace("knowledge/notes/page.md", b"after\n")],
        operation_id="replace:page",
    )

    artifact_root = state_root / "run/transactions" / transaction.id
    assert (artifact_root / "before/000000.bin").read_bytes() == b"before\n"
    assert (artifact_root / "after/000000.bin").read_bytes() == b"after\n"
    assert (artifact_root / "plan.json").is_file()
    assert {path.relative_to(artifact_root).as_posix() for path in synced} == {
        "before/000000.bin",
        "after/000000.bin",
        "plan.json",
    }
    if os.name != "nt":
        assert stat.S_IMODE(artifact_root.stat().st_mode) == 0o700
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in synced)


def test_artifacts_are_created_owner_only_without_an_umask_window(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    create_modes = []
    real_open = markdown_transaction.os.open

    def recording_open(path, flags, mode=0o777, *args, **kwargs):
        if flags & os.O_CREAT:
            create_modes.append(mode)
        return real_open(path, flags, mode, *args, **kwargs)

    monkeypatch.setattr(markdown_transaction.os, "open", recording_open)
    coordinator = MarkdownCoordinator(vault, state_root)
    coordinator.prepare(
        [MarkdownChange.create("knowledge/notes/new.md", b"new")],
        operation_id="owner-only",
    )
    assert create_modes
    assert set(create_modes) == {0o600}


def test_prepare_is_idempotent_by_operation_id(vault: Path, state_root: Path):
    coordinator = MarkdownCoordinator(vault, state_root)
    changes = [MarkdownChange.create("knowledge/notes/new.md", b"new\n")]
    first = coordinator.prepare(changes, operation_id="same")
    second = coordinator.prepare(changes, operation_id="same")
    assert second == first

    with pytest.raises(ValueError, match="operation_id"):
        coordinator.prepare(
            [MarkdownChange.create("knowledge/notes/other.md", b"other\n")],
            operation_id="same",
        )


@pytest.mark.parametrize(
    "change",
    [
        MarkdownChange.create("knowledge/notes/existing.md", b"x"),
        MarkdownChange.replace("knowledge/notes/missing.md", b"x"),
        MarkdownChange.delete("knowledge/notes/missing.md"),
    ],
)
def test_prepare_enforces_exact_absent_semantics(
    vault: Path, state_root: Path, change: MarkdownChange
):
    (vault / "knowledge/notes/existing.md").write_bytes(b"existing")
    coordinator = MarkdownCoordinator(vault, state_root)
    with pytest.raises(FileExistsError if change.kind == "create" else FileNotFoundError):
        coordinator.prepare([change], operation_id=f"bad:{change.kind}")


@pytest.mark.parametrize(
    "path",
    [
        "knowledge/daily/2026-07-13.md",
        "knowledge/notes/page.md",
        "knowledge/projects/demo/state.md",
        "knowledge/inbox/claims/claim.md",
        "knowledge/index.md",
        "knowledge/log.md",
    ],
)
def test_all_approved_markdown_targets_are_allowed(vault: Path, state_root: Path, path: str):
    coordinator = MarkdownCoordinator(vault, state_root)
    change = (
        MarkdownChange.replace(path, b"content\n")
        if path in {"knowledge/index.md", "knowledge/log.md"}
        else MarkdownChange.create(path, b"content\n")
    )
    transaction = coordinator.prepare(
        [change], operation_id=f"allowed:{path}"
    )
    assert transaction.operations[0].path == path


@pytest.mark.parametrize(
    "path",
    [
        "knowledge/raw/source.md",
        "knowledge/feedback/item.md",
        "knowledge/inbox/source.md",
        "knowledge/claims/claim.md",
        "knowledge/other.md",
        "cache/item.md",
        "knowledge/notes/not-markdown.txt",
        "knowledge/notes/page:stream.md",
        "knowledge/notes/CON.md",
        "knowledge/notes/trailing .md ",
        "../knowledge/notes/page.md",
        "knowledge/notes/../../outside.md",
        "/knowledge/notes/page.md",
        "C:/knowledge/notes/page.md",
        "knowledge\\notes\\page.md",
    ],
)
def test_other_and_unsafe_targets_are_rejected(vault: Path, state_root: Path, path: str):
    coordinator = MarkdownCoordinator(vault, state_root)
    with pytest.raises(ValueError):
        coordinator.prepare([MarkdownChange.create(path, b"x")], operation_id=f"unsafe:{path}")


def test_duplicate_targets_are_rejected(vault: Path, state_root: Path):
    coordinator = MarkdownCoordinator(vault, state_root)
    with pytest.raises(ValueError, match="duplicate"):
        coordinator.prepare(
            [
                MarkdownChange.create("knowledge/notes/a.md", b"one"),
                MarkdownChange.create("knowledge/notes/a.md", b"two"),
            ],
            operation_id="duplicate",
        )


def test_case_alias_targets_are_rejected_as_duplicates(vault: Path, state_root: Path):
    coordinator = MarkdownCoordinator(vault, state_root)
    with pytest.raises(ValueError, match="duplicate"):
        coordinator.prepare(
            [
                MarkdownChange.create("knowledge/notes/Page.md", b"one"),
                MarkdownChange.create("knowledge/notes/page.md", b"two"),
            ],
            operation_id="case-alias",
        )


def test_symlinked_target_component_is_rejected(vault: Path, state_root: Path):
    outside = vault.parent / "outside"
    outside.mkdir()
    link = vault / "knowledge/notes/link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")

    coordinator = MarkdownCoordinator(vault, state_root)
    with pytest.raises(ValueError, match="symlink|reparse"):
        coordinator.prepare(
            [MarkdownChange.create("knowledge/notes/link/escape.md", b"x")],
            operation_id="symlink",
        )


def test_windows_reparse_target_component_is_rejected(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(markdown_transaction, "_is_reparse_point", lambda path: path.name == "notes")
    coordinator = MarkdownCoordinator(vault, state_root)
    with pytest.raises(ValueError, match="reparse"):
        coordinator.prepare(
            [MarkdownChange.create("knowledge/notes/page.md", b"x")],
            operation_id="reparse",
        )


def test_callbacks_receive_closed_validated_plan_after_snapshot(
    vault: Path, state_root: Path
):
    target = vault / "knowledge/notes/page.md"
    target.write_bytes(b"before")
    observed = []

    def validator(plan):
        observed.append(plan)
        assert set(plan) == {"schema_version", "transaction_id", "operations"}
        assert set(plan["operations"][0]) == {"kind", "path", "before", "after"}
        target.write_bytes(b"external")

    coordinator = MarkdownCoordinator(vault, state_root)
    transaction = coordinator.prepare(
        [MarkdownChange.replace("knowledge/notes/page.md", b"after")],
        operation_id="callback",
        validators=(validator,),
    )
    assert observed
    assert transaction.operations[0].before_hash == sha256_bytes(b"before")
    with pytest.raises(RuntimeError, match="before state"):
        coordinator.apply(transaction.id)
    assert target.read_bytes() == b"external"


def test_callback_failure_does_not_publish_prepared_transaction(vault: Path, state_root: Path):
    def reject(plan):
        raise ValueError("invalid links")

    coordinator = MarkdownCoordinator(vault, state_root)
    with pytest.raises(ValueError, match="invalid links"):
        coordinator.prepare(
            [MarkdownChange.create("knowledge/notes/new.md", b"new")],
            operation_id="rejected",
            validators=(reject,),
        )
    with sqlite3.connect(state_root / "run/markdown-transactions.sqlite3") as database:
        assert database.execute('SELECT state FROM "transaction"').fetchall() == []


def test_prepare_rechecks_staged_artifact_hashes_after_callbacks(vault: Path, state_root: Path):
    def corrupt_after_image(plan):
        transaction_id = plan["transaction_id"]
        artifact = plan["operations"][0]["after"]["artifact"]
        (state_root / "run/transactions" / transaction_id / artifact).write_bytes(b"corrupt")

    coordinator = MarkdownCoordinator(vault, state_root)
    with pytest.raises(RuntimeError, match="artifact hash"):
        coordinator.prepare(
            [MarkdownChange.create("knowledge/notes/new.md", b"new")],
            operation_id="corrupt-artifact",
            validators=(corrupt_after_image,),
        )
    assert not (vault / "knowledge/notes/new.md").exists()


def test_persisted_preconditions_are_rechecked(vault: Path, state_root: Path):
    guard = vault / "knowledge/index.md"
    coordinator = MarkdownCoordinator(vault, state_root)
    transaction = coordinator.prepare(
        [MarkdownChange.create("knowledge/notes/new.md", b"new")],
        operation_id="precondition",
        preconditions={"knowledge/index.md": sha256_bytes(guard.read_bytes())},
    )
    guard.write_bytes(b"changed")

    with pytest.raises(RuntimeError, match="precondition"):
        coordinator.apply(transaction.id)
    assert not (vault / "knowledge/notes/new.md").exists()


def test_create_does_not_clobber_file_created_after_prepare(vault: Path, state_root: Path):
    target = vault / "knowledge/notes/new.md"
    coordinator = MarkdownCoordinator(vault, state_root)
    transaction = coordinator.prepare(
        [MarkdownChange.create("knowledge/notes/new.md", b"transaction")],
        operation_id="create",
    )
    target.write_bytes(b"external")

    with pytest.raises(RuntimeError, match="before state"):
        coordinator.apply(transaction.id)
    assert target.read_bytes() == b"external"


def test_replace_uses_random_same_directory_temp_and_os_replace(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    target = vault / "knowledge/notes/page.md"
    target.write_bytes(b"before")
    replacements = []
    real_replace = os.replace

    def recording_replace(source, destination):
        replacements.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(markdown_transaction.os, "replace", recording_replace)
    coordinator = MarkdownCoordinator(vault, state_root)
    first = coordinator.prepare(
        [MarkdownChange.replace("knowledge/notes/page.md", b"after-one")],
        operation_id="replace:one",
    )
    coordinator.apply(first.id)
    second = coordinator.prepare(
        [MarkdownChange.replace("knowledge/notes/page.md", b"after-two")],
        operation_id="replace:two",
    )
    coordinator.apply(second.id)

    assert len(replacements) == 2
    assert all(source.parent == target.parent and destination == target for source, destination in replacements)
    assert replacements[0][0].name != replacements[1][0].name
    assert not list(target.parent.glob(".*.tmp"))


def test_delete_refuses_unknown_bytes(vault: Path, state_root: Path):
    target = vault / "knowledge/notes/page.md"
    target.write_bytes(b"before")
    coordinator = MarkdownCoordinator(vault, state_root)
    transaction = coordinator.prepare(
        [MarkdownChange.delete("knowledge/notes/page.md")], operation_id="delete"
    )
    target.write_bytes(b"unknown")
    with pytest.raises(RuntimeError, match="before state"):
        coordinator.apply(transaction.id)
    assert target.read_bytes() == b"unknown"


def test_coherent_read_returns_present_and_absent_states(vault: Path, state_root: Path):
    coordinator = MarkdownCoordinator(vault, state_root)
    assert coordinator.coherent_read(
        [Path("knowledge/index.md"), Path("knowledge/notes/missing.md")]
    ) == {
        Path("knowledge/index.md"): b"index-v1\n",
        Path("knowledge/notes/missing.md"): None,
    }


def test_global_writer_gate_serializes_coordinators(vault: Path, state_root: Path):
    first = MarkdownCoordinator(vault, state_root)
    second = MarkdownCoordinator(vault, state_root)
    entered = threading.Event()
    release = threading.Event()
    order = []

    def hold_first():
        with first.writer_gate():
            order.append("first")
            entered.set()
            assert release.wait(5)

    def enter_second():
        assert entered.wait(5)
        with second.writer_gate():
            order.append("second")

    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(hold_first)
        two = pool.submit(enter_second)
        assert entered.wait(5)
        time.sleep(0.1)
        assert order == ["first"]
        release.set()
        one.result(timeout=5)
        two.result(timeout=5)
    assert order == ["first", "second"]


def test_apply_fsyncs_changed_directories(vault: Path, state_root: Path, monkeypatch):
    synced = []
    monkeypatch.setattr(markdown_transaction, "fsync_directory", lambda path: synced.append(Path(path)))
    coordinator = MarkdownCoordinator(vault, state_root)
    transaction = coordinator.prepare(
        [MarkdownChange.create("knowledge/notes/new.md", b"new")], operation_id="dirsync"
    )
    coordinator.apply(transaction.id)
    assert vault / "knowledge/notes" in synced


def test_posix_directory_fsync_unsupported_error_is_tolerated(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    real_open = markdown_transaction.os.open

    def unsupported(path, flags, *args, **kwargs):
        if Path(path) == vault / "knowledge/notes":
            raise OSError(errno.ENOTSUP, "unsupported")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr("reliable_memory.os.open", unsupported)
    coordinator = MarkdownCoordinator(vault, state_root)
    transaction = coordinator.prepare(
        [MarkdownChange.create("knowledge/notes/new.md", b"new")], operation_id="fallback"
    )
    assert coordinator.apply(transaction.id).state == "committed"


def test_windows_sharing_violation_leaves_unknown_target_unchanged(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    target = vault / "knowledge/notes/page.md"
    target.write_bytes(b"before")
    coordinator = MarkdownCoordinator(vault, state_root)
    transaction = coordinator.prepare(
        [MarkdownChange.replace("knowledge/notes/page.md", b"after")],
        operation_id="sharing",
    )

    def sharing_violation(source, destination):
        raise PermissionError(32, "sharing violation", str(destination))

    monkeypatch.setattr(markdown_transaction.os, "replace", sharing_violation)
    with pytest.raises(PermissionError, match="sharing violation"):
        coordinator.apply(transaction.id)
    assert target.read_bytes() == b"before"


def test_database_has_required_tables_and_durability_pragmas(vault: Path, state_root: Path):
    MarkdownCoordinator(vault, state_root)
    with sqlite3.connect(state_root / "run/markdown-transactions.sqlite3") as database:
        tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"transaction", "operation", "project_leases", "writer_owners", "maintenance_owners"} <= tables
        assert database.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert database.execute("PRAGMA synchronous").fetchone()[0] == 2
