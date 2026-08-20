from __future__ import annotations

import contextlib
import errno
import inspect
import json
import os
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import markdown_transaction
import pytest
from markdown_transaction import MarkdownChange, MarkdownCoordinator
from reliable_memory import canonical_json_bytes, sha256_bytes

# How long a coordination wait may take on the slowest supported machine: the
# hosted four-vCPU Windows and macOS runners, where opening a SQLite database
# and finishing one transaction has been measured well past two seconds under
# load. These waits bound a hang, not the expected duration.
_COORDINATION_BUDGET_SECONDS = 60.0


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
        "knowledge/feedback",
    ):
        (root / relative).mkdir(parents=True)
    (root / "knowledge/index.md").write_bytes(b"index-v1\n")
    (root / "knowledge/log.md").write_bytes(b"log-v1\n")
    return root


@pytest.fixture
def state_root(tmp_path: Path) -> Path:
    return tmp_path / "state"


def test_markdown_connect_context_commits_rolls_back_and_closes(
    vault: Path, state_root: Path
):
    coordinator = MarkdownCoordinator(vault, state_root)

    with pytest.raises(RuntimeError, match="rollback"):
        with coordinator._connect() as database:
            database.execute(
                "INSERT INTO maintenance_owners "
                "(owner_name, owner_token, process_id, acquired_at) VALUES (?, ?, ?, ?)",
                ("test", "token", os.getpid(), "2026-08-11T00:00:00Z"),
            )
            raise RuntimeError("rollback")

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        database.execute("SELECT 1")
    with coordinator._connect() as verification:
        assert verification.execute(
            "SELECT 1 FROM maintenance_owners WHERE owner_name='test'"
        ).fetchone() is None


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


def test_model_output_recheck_quarantines_and_rolls_back_exact_old_bytes(
    vault: Path, state_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    existing = vault / "knowledge/notes/existing.md"
    existing.write_bytes(b"legacy-sensitive-value\n")
    blocked = vault / "knowledge/notes/blocked.md"
    coordinator = MarkdownCoordinator(vault, state_root)
    transaction = coordinator.prepare(
        [
            MarkdownChange.replace("knowledge/notes/existing.md", b"safe\n"),
            MarkdownChange.create(
                "knowledge/notes/blocked.md", b"publication-secret\n"
            ),
        ],
        operation_id="compile:model-output-recheck",
        content_guard="model_output",
    )
    payload = {
        "version": 1,
        "literals": ["legacy-sensitive-value", "publication-secret"],
        "allow_fingerprints": [],
    }
    policy = {
        **payload,
        "sha256": sha256_bytes(canonical_json_bytes(payload)),
    }
    policy_path = tmp_path / "dlp-policy.json"
    policy_path.write_bytes(canonical_json_bytes(policy))
    monkeypatch.setenv("LLM_WIKI_DLP_POLICY", str(policy_path))

    with pytest.raises(markdown_transaction.TransactionFailure) as raised:
        coordinator.apply(transaction.id)

    assert raised.value.code == "dlp_content_blocked"
    assert existing.read_bytes() == b"legacy-sensitive-value\n"
    assert not blocked.exists()
    assert coordinator._record(transaction.id).state == "quarantined"


def test_model_output_recheck_quarantines_during_recovery(
    vault: Path, state_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    coordinator = MarkdownCoordinator(vault, state_root)
    transaction = coordinator.prepare(
        [
            MarkdownChange.create(
                "knowledge/notes/recovered.md", b"recovery-publication-secret\n"
            )
        ],
        operation_id="compile:model-output-recovery",
        content_guard="model_output",
    )
    payload = {
        "version": 1,
        "literals": ["recovery-publication-secret"],
        "allow_fingerprints": [],
    }
    policy = {
        **payload,
        "sha256": sha256_bytes(canonical_json_bytes(payload)),
    }
    policy_path = tmp_path / "recovery-dlp-policy.json"
    policy_path.write_bytes(canonical_json_bytes(policy))
    monkeypatch.setenv("LLM_WIKI_DLP_POLICY", str(policy_path))

    recovered = MarkdownCoordinator(vault, state_root).recover()

    assert [record.id for record in recovered] == [transaction.id]
    assert recovered[0].state == "quarantined"
    assert not (vault / "knowledge/notes/recovered.md").exists()


@pytest.mark.parametrize(
    "identity",
    [0, (1 << 63) - 1, 1 << 63, 11853635609087352826, (1 << 64) - 1],
)
def test_sqlite_filesystem_identity_encoding_round_trips_without_collisions(identity: int):
    encoded = markdown_transaction._encode_filesystem_id(identity)

    assert -(1 << 63) <= encoded < 1 << 63
    assert markdown_transaction._decode_filesystem_id(encoded) == identity
    assert len(
        {
            markdown_transaction._encode_filesystem_id(0),
            markdown_transaction._encode_filesystem_id(1 << 63),
        }
    ) == 2


def _stat_with_identity(metadata: os.stat_result, identity: tuple[int, int]) -> object:
    return SimpleNamespace(
        st_mode=metadata.st_mode,
        st_ino=identity[1],
        st_dev=identity[0],
        st_size=metadata.st_size,
        st_mtime_ns=metadata.st_mtime_ns,
        st_ctime_ns=metadata.st_ctime_ns,
        st_file_attributes=getattr(metadata, "st_file_attributes", 0),
    )


@pytest.mark.skipif(os.name != "nt", reason="requires Windows stat semantics")
def test_windows_capture_snapshot_ignores_deprecated_creation_time():
    left = SimpleNamespace(
        st_mode=stat.S_IFREG,
        st_ino=2,
        st_dev=1,
        st_size=7,
        st_mtime_ns=3,
        st_ctime_ns=4,
    )
    right = SimpleNamespace(**vars(left))
    right.st_ctime_ns = 5

    assert MarkdownCoordinator._same_capture_snapshot(left, right)


def test_read_normalizes_equivalent_signed_and_unsigned_stat_identities(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    target = vault / "knowledge/notes/page.md"
    target.write_bytes(b"content")
    coordinator = MarkdownCoordinator(vault, state_root)
    identity = (11853635609087352826, (1 << 63) + 7)
    encoded = tuple(markdown_transaction._encode_filesystem_id(value) for value in identity)
    real_lstat = os.lstat
    real_fstat = os.fstat
    monkeypatch.setattr(
        os,
        "lstat",
        lambda path: _stat_with_identity(real_lstat(path), identity),
    )
    monkeypatch.setattr(
        os,
        "fstat",
        lambda descriptor: _stat_with_identity(real_fstat(descriptor), encoded),
    )

    assert coordinator._read_target(target) == b"content"


def test_hash_normalizes_equivalent_signed_and_unsigned_stat_identities(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    target = vault / "knowledge/notes/page.md"
    target.write_bytes(b"content")
    coordinator = MarkdownCoordinator(vault, state_root)
    identity = (11853635609087352826, (1 << 63) + 7)
    encoded = tuple(markdown_transaction._encode_filesystem_id(value) for value in identity)
    real_lstat = os.lstat
    real_fstat = os.fstat
    monkeypatch.setattr(
        os,
        "lstat",
        lambda path: _stat_with_identity(real_lstat(path), identity),
    )
    monkeypatch.setattr(
        os,
        "fstat",
        lambda descriptor: _stat_with_identity(real_fstat(descriptor), encoded),
    )

    assert coordinator._hash_bounded_target(target) == sha256_bytes(b"content")


def test_prepare_apply_preserves_real_parent_identity(vault: Path, state_root: Path):
    target = vault / "knowledge/notes/page.md"
    target.write_bytes(b"before")
    coordinator = MarkdownCoordinator(vault, state_root)
    expected = coordinator._parent_identity(target.parent)

    transaction = coordinator.prepare(
        [MarkdownChange.replace("knowledge/notes/page.md", b"after")],
        operation_id="real-parent-identity",
    )

    with sqlite3.connect(coordinator.database_path) as database:
        persisted = database.execute(
            'SELECT parent_device, parent_inode FROM "operation" WHERE transaction_id = ?',
            (transaction.id,),
        ).fetchone()
    assert markdown_transaction._decode_parent_identity(persisted) == expected
    assert coordinator.apply(transaction.id).state == "committed"
    assert target.read_bytes() == b"after"


def test_prepare_persists_unsigned_64_bit_parent_identity(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    target = vault / "knowledge/notes/page.md"
    target.write_bytes(b"before")
    coordinator = MarkdownCoordinator(vault, state_root)
    parent_identity = (11853635609087352826, (1 << 63) + 7)

    def capture_with_unsigned_identity(path: Path, *, max_before_bytes: int | None = None):
        return path.read_bytes(), parent_identity

    monkeypatch.setattr(coordinator, "_capture_target", capture_with_unsigned_identity)
    monkeypatch.setattr(coordinator, "_parent_identity", lambda path: parent_identity)

    transaction = coordinator.prepare(
        [MarkdownChange.replace("knowledge/notes/page.md", b"after")],
        operation_id="unsigned-parent-identity",
    )

    with sqlite3.connect(coordinator.database_path) as database:
        persisted = database.execute(
            'SELECT parent_device, parent_inode FROM "operation" WHERE transaction_id = ?',
            (transaction.id,),
        ).fetchone()
    assert persisted == tuple(
        markdown_transaction._encode_filesystem_id(value) for value in parent_identity
    )
    manifest = json.loads(
        (state_root / "run/transactions" / transaction.id / "manifest.json").read_bytes()
    )
    assert (
        manifest["operations"][0]["parent_device"],
        manifest["operations"][0]["parent_inode"],
    ) == parent_identity

    assert transaction.state == "prepared"
    assert target.read_bytes() == b"before"


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
        "manifest.json",
    }
    if os.name != "nt":
        assert stat.S_IMODE(artifact_root.stat().st_mode) == 0o700
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in synced)


def test_prepare_rejects_oversized_before_image_without_artifact(
    vault: Path, state_root: Path
):
    target = vault / "knowledge/notes/page.md"
    target.write_bytes(b"12345")
    coordinator = MarkdownCoordinator(vault, state_root)

    with pytest.raises(ValueError, match="exceeds 4 bytes"):
        coordinator.prepare(
            [
                MarkdownChange.replace(
                    "knowledge/notes/page.md",
                    b"after",
                    max_before_bytes=4,
                )
            ],
            operation_id="bounded-before",
        )

    assert list(coordinator.transaction_root.iterdir()) == []
    with coordinator._connect() as database:
        assert database.execute('SELECT * FROM "transaction"').fetchall() == []


def test_apply_hashes_oversized_changed_target_without_materializing_it(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    target = vault / "knowledge/guardrails.md"
    target.write_bytes(b"before")
    monkeypatch.setattr(markdown_transaction, "MAX_KNOWLEDGE_TARGET_BYTES", 8)
    coordinator = MarkdownCoordinator(vault, state_root)
    transaction = coordinator.prepare(
        [MarkdownChange.replace("knowledge/guardrails.md", b"after")],
        operation_id="oversized-before-apply",
    )
    target.write_bytes(b"x" * 9)
    real_sha256_bytes = markdown_transaction.sha256_bytes

    def reject_materialized_oversize(value: bytes) -> str:
        assert value != b"x" * 9, "oversized target was materialized for hashing"
        return real_sha256_bytes(value)

    monkeypatch.setattr(markdown_transaction, "sha256_bytes", reject_materialized_oversize)

    with pytest.raises(markdown_transaction.TransactionFailure) as raised:
        coordinator.apply(transaction.id)

    assert raised.value.code == "before_hash_mismatch"
    assert target.read_bytes() == b"x" * 9


def test_prepare_rejects_target_replaced_between_lstat_and_open(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    target = vault / "knowledge/notes/page.md"
    target.write_bytes(b"before")
    replacement = target.with_name("replacement.tmp")
    real_open = markdown_transaction.os.open
    replaced = False

    def replace_before_open(path, flags, *args, **kwargs):
        nonlocal replaced
        if Path(path).name == target.name and not replaced:
            replacement.write_bytes(b"external")
            os.replace(replacement, target)
            replaced = True
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(markdown_transaction.os, "open", replace_before_open)
    coordinator = MarkdownCoordinator(vault, state_root)

    with pytest.raises(ValueError, match="changed before open"):
        coordinator.prepare(
            [
                MarkdownChange.replace(
                    "knowledge/notes/page.md",
                    b"after",
                    max_before_bytes=16,
                )
            ],
            operation_id="replaced-before-open",
        )

    assert list(coordinator.transaction_root.iterdir()) == []


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
    assert second.id == first.id
    assert second.state == "committed"

    with pytest.raises(ValueError, match="operation_id"):
        coordinator.prepare(
            [MarkdownChange.create("knowledge/notes/other.md", b"other\n")],
            operation_id="same",
        )


def test_operation_lookup_tolerates_disappearing_preparing_row(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator = MarkdownCoordinator(vault, state_root)
    with coordinator._connect() as database:
        database.execute(
            'INSERT INTO "transaction" '
            "(id,operation_id,request_hash,state,preconditions_json,plan_hash,"
            "created_at,updated_at,owner_pid) "
            "VALUES ('transient','transient-operation','request','preparing',"
            "'{}','','now','now',?)",
            (os.getpid(),),
        )
        database.commit()
    original = coordinator._record

    def delete_then_read(transaction_id: str):
        with coordinator._connect() as database:
            database.execute(
                'DELETE FROM "transaction" WHERE id=? AND state=\'preparing\'',
                (transaction_id,),
            )
            database.commit()
        return original(transaction_id)

    monkeypatch.setattr(coordinator, "_record", delete_then_read)

    assert coordinator._record_for_operation_id("transient-operation") is None


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
        "knowledge/inbox/source.md",
        "knowledge/feedback/abcdef123456.json",
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
    if os.name == "nt":
        real_replace = markdown_transaction.durable_publish_file

        def recording_replace(staged, destination, **options):
            replacements.append((Path(staged), Path(destination), options))
            return real_replace(staged, destination, **options)

        monkeypatch.setattr(markdown_transaction, "durable_publish_file", recording_replace)
    else:
        real_replace = os.replace

        def recording_replace(source, destination, *args, **kwargs):
            replacements.append((Path(source), Path(destination)))
            real_replace(source, destination, *args, **kwargs)

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
    if os.name == "nt":
        assert all(destination == target for _staged, destination, _options in replacements)
        assert all(options["replace"] is True for _staged, _destination, options in replacements)
        assert replacements[0][0] != replacements[1][0]
    elif markdown_transaction._use_posix_dir_fd():
        assert all(source.parent == Path() and destination == Path(target.name) for source, destination in replacements)
    else:
        assert all(
            source.parent == target.parent and destination == target
            for source, destination in replacements
        )
    if os.name != "nt":
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


def test_apply_recovers_after_filesystem_mutation_before_applied_update(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    target = vault / "knowledge/notes/page.md"
    target.write_bytes(b"before")
    coordinator = MarkdownCoordinator(vault, state_root)
    transaction = coordinator.prepare(
        [MarkdownChange.replace("knowledge/notes/page.md", b"after")],
        operation_id="crash-after-mutation",
        preconditions={"knowledge/notes/page.md": sha256_bytes(b"before")},
    )
    mark_applied = coordinator._mark_operation_applied
    injected = False

    def fail_once(transaction_id, position):
        nonlocal injected
        if not injected:
            injected = True
            raise RuntimeError("injected after filesystem mutation")
        mark_applied(transaction_id, position)

    monkeypatch.setattr(coordinator, "_mark_operation_applied", fail_once)
    with pytest.raises(RuntimeError, match="injected after filesystem mutation"):
        coordinator.apply(transaction.id)
    assert target.read_bytes() == b"after"
    with sqlite3.connect(state_root / "run/markdown-transactions.sqlite3") as database:
        assert database.execute(
            'SELECT applied FROM "operation" WHERE transaction_id = ?', (transaction.id,)
        ).fetchone()[0] == 0

    monkeypatch.setattr(coordinator, "_mark_operation_applied", mark_applied)
    assert coordinator.apply(transaction.id).state == "committed"
    with sqlite3.connect(state_root / "run/markdown-transactions.sqlite3") as database:
        assert database.execute(
            'SELECT applied FROM "operation" WHERE transaction_id = ?', (transaction.id,)
        ).fetchone()[0] == 1


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
            assert release.wait(_COORDINATION_BUDGET_SECONDS)

    def enter_second():
        assert entered.wait(_COORDINATION_BUDGET_SECONDS)
        with second.writer_gate():
            order.append("second")

    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(hold_first)
        two = pool.submit(enter_second)
        assert entered.wait(_COORDINATION_BUDGET_SECONDS)
        time.sleep(0.1)
        assert order == ["first"]
        with sqlite3.connect(state_root / "run/markdown-transactions.sqlite3") as database:
            owner = database.execute(
                "SELECT owner_token, process_id, heartbeat_at, expires_at, fencing_epoch "
                "FROM writer_owners WHERE gate_name = 'global'"
            ).fetchone()
        assert owner is not None
        assert owner[1] == os.getpid()
        assert all(owner[index] for index in (0, 2, 3))
        assert owner[4] >= 1
        release.set()
        one.result(timeout=5)
        two.result(timeout=5)
    assert order == ["first", "second"]


def test_nested_writer_references_parent_without_reacquiring_or_releasing_it(
    tmp_path: Path,
) -> None:
    import operational_ownership

    state_root = tmp_path / "state"
    candidate = state_root / "run/markdown-transactions-v3.candidate.sqlite3"
    markdown_transaction.initialize_coordinator_v3_candidate(candidate, source_v2=None)
    coordinator = MarkdownCoordinator._from_v3_candidate(candidate, state_root=state_root)
    registry = operational_ownership.OwnershipRegistry(state_root)
    parent = registry.acquire("doctor", scope="global")

    with coordinator.writer_gate(owner=parent) as projected:
        assert projected == parent
        with coordinator.writer_gate(owner=parent) as reentrant:
            assert reentrant == parent
            with sqlite3.connect(candidate) as database:
                assert database.execute(
                    "SELECT canonical_role, canonical_scope, owner_token, fencing_epoch "
                    "FROM writer_owners WHERE gate_name='global'"
                ).fetchone() == ("doctor", "global", parent.token, parent.epoch)
                assert database.execute(
                    "SELECT COUNT(*) FROM maintenance_owners"
                ).fetchone() == (1,)

        with sqlite3.connect(candidate) as database:
            assert database.execute(
                "SELECT COUNT(*) FROM writer_owners"
            ).fetchone() == (1,)

    with sqlite3.connect(candidate) as database:
        assert database.execute("SELECT COUNT(*) FROM writer_owners").fetchone() == (0,)
        assert database.execute(
            "SELECT owner_token, fencing_epoch FROM maintenance_owners "
            "WHERE role='doctor' AND scope='global'"
        ).fetchone() == (parent.token, parent.epoch)
    registry.release(parent)


def test_v3_top_level_writer_inserts_and_releases_canonical_projection(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    candidate = state_root / "run/markdown-transactions-v3.candidate.sqlite3"
    markdown_transaction.initialize_coordinator_v3_candidate(candidate, source_v2=None)
    coordinator = MarkdownCoordinator._from_v3_candidate(candidate, state_root=state_root)

    with coordinator.writer_gate() as owner:
        assert owner.role == "markdown-writer"
        assert owner.scope == "global"
        with coordinator._connect() as database:
            coordinator._assert_writer_ownership(database)
        with sqlite3.connect(candidate) as database:
            assert database.execute(
                "SELECT canonical_role, canonical_scope, actor_id, owner_token, "
                "fencing_epoch FROM writer_owners WHERE gate_name='global'"
            ).fetchone() == database.execute(
                "SELECT role, scope, actor_id, owner_token, fencing_epoch "
                "FROM maintenance_owners WHERE role='markdown-writer' AND scope='global'"
            ).fetchone()
            assert database.execute(
                "SELECT heartbeat_at, expires_at FROM writer_owners "
                "WHERE gate_name='global'"
            ).fetchone() == database.execute(
                "SELECT heartbeat_at, expires_at FROM maintenance_owners "
                "WHERE role='markdown-writer' AND scope='global'"
            ).fetchone()

    with sqlite3.connect(candidate) as database:
        assert database.execute("SELECT COUNT(*) FROM writer_owners").fetchone() == (0,)
        assert database.execute(
            "SELECT COUNT(*) FROM maintenance_owners WHERE role='markdown-writer'"
        ).fetchone() == (0,)


def test_crashed_writer_owner_is_reclaimed_with_higher_fence(vault: Path, state_root: Path):
    coordinator = MarkdownCoordinator(vault, state_root)
    marker = state_root / "child-acquired"
    code = """
import os
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from markdown_transaction import MarkdownCoordinator
coordinator = MarkdownCoordinator(Path(sys.argv[2]), Path(sys.argv[3]))
with coordinator.writer_gate():
    Path(sys.argv[4]).write_text('acquired', encoding='utf-8')
    os._exit(0)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(Path(markdown_transaction.__file__).parent),
            str(vault),
            str(state_root),
            str(marker),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert marker.is_file()
    with sqlite3.connect(state_root / "run/markdown-transactions.sqlite3") as database:
        crashed = database.execute(
            "SELECT process_id, owner_token, fencing_epoch FROM writer_owners "
            "WHERE gate_name = 'global'"
        ).fetchone()
    assert crashed is not None

    with coordinator.writer_gate():
        with sqlite3.connect(state_root / "run/markdown-transactions.sqlite3") as database:
            recovered = database.execute(
                "SELECT process_id, owner_token, fencing_epoch FROM writer_owners "
                "WHERE gate_name = 'global'"
            ).fetchone()
        assert recovered[0] == os.getpid()
        assert recovered[1] != crashed[1]
        assert recovered[2] > crashed[2]


def test_live_writer_heartbeat_renews_before_expiry(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(markdown_transaction, "_WRITER_LEASE_SECONDS", 0.3)
    monkeypatch.setattr(markdown_transaction, "_WRITER_HEARTBEAT_SECONDS", 0.05)
    coordinator = MarkdownCoordinator(vault, state_root)
    with coordinator.writer_gate():
        with sqlite3.connect(state_root / "run/markdown-transactions.sqlite3") as database:
            first = database.execute(
                "SELECT owner_token, heartbeat_at, fencing_epoch FROM writer_owners "
                "WHERE gate_name = 'global'"
            ).fetchone()
        time.sleep(0.4)
        with sqlite3.connect(state_root / "run/markdown-transactions.sqlite3") as database:
            renewed = database.execute(
                "SELECT owner_token, heartbeat_at, fencing_epoch FROM writer_owners "
                "WHERE gate_name = 'global'"
            ).fetchone()
        assert renewed[0] == first[0]
        assert renewed[1] > first[1]
        assert renewed[2] == first[2]


def test_late_writer_heartbeat_renews_expired_unchanged_owner(
    vault: Path, state_root: Path
):
    coordinator = MarkdownCoordinator(vault, state_root)
    token = "late-heartbeat-owner"
    expired = "2000-01-01T00:00:00Z"
    with coordinator._connect() as database:
        database.execute(
            "INSERT INTO writer_owners VALUES ('global', ?, ?, ?, ?, ?, ?, ?)",
            (token, os.getpid(), threading.get_ident(), expired, expired, expired, 1),
        )
        database.commit()
    waits = 0

    def stop_after_one_heartbeat(_timeout: float) -> bool:
        nonlocal waits
        waits += 1
        return waits > 1

    lost = threading.Event()
    coordinator._heartbeat_writer_gate(
        token, 1, SimpleNamespace(wait=stop_after_one_heartbeat), lost
    )

    with coordinator._connect() as database:
        renewed = database.execute(
            "SELECT owner_token, heartbeat_at, expires_at, fencing_epoch "
            "FROM writer_owners WHERE gate_name = 'global'"
        ).fetchone()
    assert not lost.is_set()
    assert tuple(renewed) == (token, renewed[1], renewed[2], 1)
    assert renewed[1] > expired
    assert renewed[2] > renewed[1]


def test_late_writer_heartbeat_after_takeover_is_lost_without_overwrite(
    vault: Path, state_root: Path
):
    coordinator = MarkdownCoordinator(vault, state_root)
    takeover = (
        "new-owner",
        os.getpid(),
        threading.get_ident(),
        "2026-07-20T12:00:00Z",
        "2026-07-20T12:00:01Z",
        "2099-01-01T00:00:00Z",
        2,
    )
    with coordinator._connect() as database:
        database.execute(
            "INSERT INTO writer_owners VALUES ('global', ?, ?, ?, ?, ?, ?, ?)",
            takeover,
        )
        database.commit()

    lost = threading.Event()
    coordinator._heartbeat_writer_gate(
        "old-owner", 1, SimpleNamespace(wait=lambda _timeout: False), lost
    )

    with coordinator._connect() as database:
        current = database.execute(
            "SELECT owner_token, process_id, thread_id, acquired_at, heartbeat_at, "
            "expires_at, fencing_epoch FROM writer_owners WHERE gate_name = 'global'"
        ).fetchone()
    assert lost.is_set()
    assert tuple(current) == takeover


@pytest.mark.parametrize(
    "contention",
    [
        sqlite3.OperationalError("database is busy"),
        sqlite3.OperationalError("database table is locked"),
        PermissionError(32, "sharing violation"),
    ],
)
def test_writer_heartbeat_retries_transient_contention_without_losing_fence(
    vault: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    contention: BaseException,
):
    coordinator = MarkdownCoordinator(vault, state_root)
    token = "heartbeat-owner"
    now = markdown_transaction._now()
    with coordinator._connect() as database:
        database.execute(
            "INSERT INTO writer_owners VALUES ('global', ?, ?, ?, ?, ?, ?, ?)",
            (
                token,
                os.getpid(),
                threading.get_ident(),
                now,
                now,
                markdown_transaction._future_timestamp(1),
                1,
            ),
        )
        database.commit()

    real_connect = coordinator._connect
    connected = threading.Event()
    attempts = 0

    def contended_connect(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise contention
        connected.set()
        return real_connect(**kwargs)

    monkeypatch.setattr(markdown_transaction, "_WRITER_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(coordinator, "_connect", contended_connect)
    stop = threading.Event()
    lost = threading.Event()
    thread = threading.Thread(
        target=coordinator._heartbeat_writer_gate,
        args=(token, 1, stop, lost),
    )
    thread.start()
    assert connected.wait(_COORDINATION_BUDGET_SECONDS)
    stop.set()
    thread.join(timeout=_COORDINATION_BUDGET_SECONDS)

    assert not thread.is_alive()
    assert attempts >= 3
    assert not lost.is_set()


def test_adoption_validation_recognizes_nested_sqlite_contention() -> None:
    contention = sqlite3.OperationalError("database is locked")
    wrapped = RuntimeError("reliability_v3_record_invalid")
    wrapped.__cause__ = contention

    assert markdown_transaction._transient_adoption_contention(wrapped)


def test_writer_gate_exit_retries_locked_database_then_releases_owner(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    coordinator = MarkdownCoordinator(vault, state_root)
    real_begin = markdown_transaction.begin_immediate
    main_thread = threading.get_ident()
    armed = False
    injected = False

    @contextlib.contextmanager
    def contended_begin(database):
        nonlocal injected
        if armed and threading.get_ident() == main_thread and not injected:
            injected = True
            raise sqlite3.OperationalError("database is locked")
        with real_begin(database):
            yield database

    monkeypatch.setattr(markdown_transaction, "begin_immediate", contended_begin)
    with coordinator.writer_gate():
        armed = True

    assert injected
    with sqlite3.connect(coordinator.database_path) as database:
        assert database.execute("SELECT * FROM writer_owners").fetchall() == []


def test_a_heartbeat_that_failed_does_not_fail_a_write_this_owner_still_holds(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    """A busy database briefly starving the heartbeat is contention, not a loss.

    Hosted runners produced `Markdown writer gate ownership was lost` while the
    owner still held the gate row, which failed the caller's write for nothing.
    Only a reclaim — which deletes this owner's row and bumps the fence — is a
    real loss.
    """
    target = vault / "knowledge/notes/page.md"
    target.write_bytes(b"before")
    coordinator = MarkdownCoordinator(vault, state_root)
    transaction = coordinator.prepare(
        [MarkdownChange.replace("knowledge/notes/page.md", b"after")],
        operation_id="heartbeat-starved",
    )

    def starved_heartbeat(owner_token, fencing_epoch, stop, lost):
        lost.set()

    monkeypatch.setattr(coordinator, "_heartbeat_writer_gate", starved_heartbeat)

    coordinator.apply(transaction.id)

    assert target.read_bytes() == b"after"
    with sqlite3.connect(coordinator.database_path) as database:
        assert database.execute("SELECT * FROM writer_owners").fetchall() == []


def test_a_reclaimed_gate_still_reports_the_loss_and_names_the_heartbeat(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    """Losing the row to another owner remains a hard failure with a reason."""
    coordinator = MarkdownCoordinator(vault, state_root)

    def starved_heartbeat(owner_token, fencing_epoch, stop, lost):
        lost.set()

    monkeypatch.setattr(coordinator, "_heartbeat_writer_gate", starved_heartbeat)

    with pytest.raises(RuntimeError, match="another owner reclaimed the gate"):
        with coordinator.writer_gate():
            with sqlite3.connect(coordinator.database_path) as database:
                database.execute("DELETE FROM writer_owners WHERE gate_name = 'global'")
                database.commit()


def test_reclaimed_writer_fails_fence_before_filesystem_mutation(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    target = vault / "knowledge/notes/page.md"
    target.write_bytes(b"before")
    old = MarkdownCoordinator(vault, state_root)
    new = MarkdownCoordinator(vault, state_root)
    transaction = old.prepare(
        [MarkdownChange.replace("knowledge/notes/page.md", b"after")],
        operation_id="expired-writer",
    )
    monkeypatch.setattr(old, "_heartbeat_writer_gate", lambda *args: None)
    old_gate = old.writer_gate()
    old_gate.__enter__()
    with sqlite3.connect(state_root / "run/markdown-transactions.sqlite3") as database:
        database.execute(
            "UPDATE writer_owners SET expires_at = '2000-01-01T00:00:00Z' "
            "WHERE gate_name = 'global'"
        )
        database.commit()

    new_gate = new.writer_gate()
    new_gate.__enter__()
    try:
        with pytest.raises(RuntimeError, match="writer gate ownership"):
            old.apply(transaction.id)
        assert target.read_bytes() == b"before"
    finally:
        new_gate.__exit__(None, None, None)
        with pytest.raises(RuntimeError, match="writer gate ownership"):
            old_gate.__exit__(None, None, None)


def test_expired_writer_without_successor_fails_before_filesystem_mutation(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    target = vault / "knowledge/notes/page.md"
    target.write_bytes(b"before")
    coordinator = MarkdownCoordinator(vault, state_root)
    transaction = coordinator.prepare(
        [MarkdownChange.replace("knowledge/notes/page.md", b"after")],
        operation_id="expired-without-successor",
    )
    monkeypatch.setattr(coordinator, "_heartbeat_writer_gate", lambda *args: None)

    with coordinator.writer_gate():
        with sqlite3.connect(coordinator.database_path) as database:
            database.execute(
                "UPDATE writer_owners SET expires_at = '2000-01-01T00:00:00Z' "
                "WHERE gate_name = 'global'"
            )
            database.commit()
        with pytest.raises(RuntimeError, match="ownership was lost before mutation"):
            coordinator.apply(transaction.id)

    assert target.read_bytes() == b"before"


def test_writer_ownership_renewal_rejects_expiry_equal_to_captured_now(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    coordinator = MarkdownCoordinator(vault, state_root)
    fixed_now = "2026-07-15T12:00:00Z"
    monkeypatch.setattr(coordinator, "_heartbeat_writer_gate", lambda *args: None)

    with coordinator.writer_gate():
        with sqlite3.connect(coordinator.database_path) as database:
            database.execute(
                "UPDATE writer_owners SET expires_at = ? WHERE gate_name = 'global'",
                (fixed_now,),
            )
            database.commit()
        monkeypatch.setattr(markdown_transaction, "_now", lambda: fixed_now)
        with coordinator._connect() as database, markdown_transaction.begin_immediate(database):
            with pytest.raises(RuntimeError, match="ownership was lost before mutation"):
                coordinator._assert_writer_ownership(database)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX dir_fd semantics")
def test_parent_swap_cannot_redirect_replace_outside_vault(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    notes = vault / "knowledge/notes"
    original = vault / "knowledge/notes-original"
    outside = vault.parent / "outside"
    outside.mkdir()
    target = notes / "page.md"
    target.write_bytes(b"before")
    (outside / "page.md").write_bytes(b"outside")
    coordinator = MarkdownCoordinator(vault, state_root)
    transaction = coordinator.prepare(
        [MarkdownChange.replace("knowledge/notes/page.md", b"after")],
        operation_id="parent-swap",
    )

    def swap_parent(path):
        notes.rename(original)
        notes.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(coordinator, "_before_target_mutation", swap_parent)
    with pytest.raises(RuntimeError, match="parent identity"):
        coordinator.apply(transaction.id)
    assert (outside / "page.md").read_bytes() == b"outside"


@pytest.mark.skipif(os.name != "nt", reason="requires Windows directory handles")
def test_windows_parent_identity_change_fails_closed(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    target = vault / "knowledge/notes/page.md"
    target.write_bytes(b"before")
    coordinator = MarkdownCoordinator(vault, state_root)
    transaction = coordinator.prepare(
        [MarkdownChange.replace("knowledge/notes/page.md", b"after")],
        operation_id="parent-identity",
    )
    real_identity = coordinator._parent_identity
    calls = 0

    def changing_identity(path):
        nonlocal calls
        calls += 1
        identity = real_identity(path)
        return identity if calls == 1 else (identity[0], identity[1] + 1)

    monkeypatch.setattr(markdown_transaction, "_use_posix_dir_fd", lambda: False)
    monkeypatch.setattr(coordinator, "_parent_identity", changing_identity)
    with pytest.raises(RuntimeError, match="parent identity"):
        coordinator.apply(transaction.id)
    assert target.read_bytes() == b"before"


@pytest.mark.skipif(os.name != "nt", reason="requires Windows directory sharing semantics")
def test_windows_parent_swap_cannot_redirect_handle_relative_mutation(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    notes = vault / "knowledge/notes"
    original = vault / "knowledge/notes-original"
    outside = vault.parent / "outside"
    outside.mkdir()
    target = notes / "page.md"
    target.write_bytes(b"before")
    (outside / "page.md").write_bytes(b"outside")
    coordinator = MarkdownCoordinator(vault, state_root)
    transaction = coordinator.prepare(
        [MarkdownChange.replace("knowledge/notes/page.md", b"after")],
        operation_id="windows-parent-swap",
    )
    blocked = []
    swapped = []

    def attempt_swap(path):
        try:
            notes.rename(original)
            outside.rename(notes)
            swapped.append(True)
        except OSError:
            blocked.append(True)

    monkeypatch.setattr(coordinator, "_before_target_mutation", attempt_swap)
    try:
        result = coordinator.apply(transaction.id)
    except RuntimeError as exc:
        assert swapped == [True]
        assert "parent identity" in str(exc)
        assert (notes / "page.md").read_bytes() == b"outside"
        assert (original / "page.md").read_bytes() == b"before"
    else:
        assert blocked == [True]
        assert result.state == "committed"
        assert target.read_bytes() == b"after"
        assert (outside / "page.md").read_bytes() == b"outside"


def test_windows_acl_hardening_verifies_owner_only_success(tmp_path: Path, monkeypatch):
    path = tmp_path / "artifact"
    path.write_bytes(b"artifact")
    calls = []

    def successful(command):
        calls.append(command)
        if len(calls) == 1:
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, "artifact DOMAIN\\user:(F)\n", "")

    monkeypatch.setattr(markdown_transaction, "_windows_acl_identity", lambda: "DOMAIN\\user")
    monkeypatch.setattr(markdown_transaction, "_run_acl_command", successful)
    markdown_transaction._harden_windows_acl(path)
    assert len(calls) == 2
    assert calls[0][0].casefold() == "icacls"
    assert "DOMAIN\\user:(F)" in calls[0]
    assert "/remove:g" in calls[0]
    for sid in (
        "*S-1-3-4",
        "*S-1-5-18",
        "*S-1-5-32-544",
        "*S-1-5-32-545",
        "*S-1-5-11",
    ):
        assert sid in calls[0]


@pytest.mark.skipif(os.name != "nt", reason="requires Windows ACL identity")
def test_windows_acl_hardening_failure_is_not_silently_accepted(tmp_path: Path, monkeypatch):
    path = tmp_path / "artifact"
    path.write_bytes(b"artifact")
    denied = subprocess.CompletedProcess(["icacls"], 5, "", "access denied")
    monkeypatch.setattr(markdown_transaction, "_run_acl_command", lambda command: denied)
    with pytest.raises(PermissionError, match="owner-only ACL"):
        markdown_transaction._harden_windows_acl(path)


@pytest.mark.skipif(os.name != "nt", reason="requires Windows ACL enforcement")
def test_windows_acl_failure_aborts_transaction_preparation(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    denied = subprocess.CompletedProcess(["icacls"], 5, b"", b"access denied")
    monkeypatch.setattr(markdown_transaction, "_run_acl_command", lambda command: denied)
    coordinator = MarkdownCoordinator(vault, state_root)
    with pytest.raises(PermissionError, match="owner-only ACL"):
        coordinator.prepare(
            [MarkdownChange.create("knowledge/notes/new.md", b"new")],
            operation_id="acl-denied",
        )
    assert list((state_root / "run/transactions").iterdir()) == []


def test_apply_fsyncs_changed_directories(vault: Path, state_root: Path, monkeypatch):
    synced = []
    synced_descriptors = []
    real_fsync = markdown_transaction.os.fsync

    def recording_fsync(descriptor):
        metadata = os.fstat(descriptor)
        synced_descriptors.append((metadata.st_dev, metadata.st_ino))
        real_fsync(descriptor)

    monkeypatch.setattr(markdown_transaction, "fsync_directory", lambda path: synced.append(Path(path)))
    monkeypatch.setattr(markdown_transaction.os, "fsync", recording_fsync)
    coordinator = MarkdownCoordinator(vault, state_root)
    transaction = coordinator.prepare(
        [MarkdownChange.create("knowledge/notes/new.md", b"new")], operation_id="dirsync"
    )
    coordinator.apply(transaction.id)
    if markdown_transaction._use_posix_dir_fd():
        metadata = (vault / "knowledge/notes").stat()
        assert (metadata.st_dev, metadata.st_ino) in synced_descriptors
    else:
        assert (vault / "knowledge/notes/new.md").read_bytes() == b"new"


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory fsync contract")
def test_posix_directory_fsync_unsupported_is_durability_failure(
    vault: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
):
    from reliable_memory import MetadataDurabilityUnavailable

    real_open = markdown_transaction.os.open

    def unsupported(path, flags, *args, **kwargs):
        if Path(path) == vault / "knowledge/notes":
            raise OSError(errno.ENOTSUP, "unsupported")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr("reliable_memory.os.open", unsupported)
    with pytest.raises(MetadataDurabilityUnavailable) as raised:
        markdown_transaction.fsync_directory(vault / "knowledge/notes")
    assert raised.value.code == "metadata_durability_unavailable"


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

    def sharing_violation(*args, **kwargs):
        raise PermissionError(32, "sharing violation", str(args[-1]))

    if os.name == "nt":
        monkeypatch.setattr(markdown_transaction, "durable_publish_file", sharing_violation)
    else:
        monkeypatch.setattr(markdown_transaction.os, "replace", sharing_violation)
    with pytest.raises(PermissionError, match="sharing violation"):
        coordinator.apply(transaction.id)
    assert target.read_bytes() == b"before"


def test_database_has_required_tables_and_durability_pragmas(vault: Path, state_root: Path):
    MarkdownCoordinator(vault, state_root)
    with sqlite3.connect(state_root / "run/markdown-transactions.sqlite3") as database:
        tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {
            "transaction",
            "operation",
            "project_leases",
            "writer_owners",
            "writer_fences",
            "maintenance_owners",
        } <= tables
        owner_columns = {
            row[1] for row in database.execute("PRAGMA table_info(writer_owners)")
        }
        assert {"owner_token", "process_id", "heartbeat_at", "expires_at", "fencing_epoch"} <= owner_columns
        operation_columns = {
            row[1] for row in database.execute('PRAGMA table_info("operation")')
        }
        assert {"parent_device", "parent_inode"} <= operation_columns
        assert database.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert database.execute("PRAGMA synchronous").fetchone()[0] == 2


def test_coordinator_candidate_transaction_state_accepts_abort_states_only(
    tmp_path: Path,
) -> None:
    path = tmp_path / "run" / "markdown-transactions-v3.candidate.sqlite3"
    markdown_transaction.initialize_coordinator_v3_candidate(path, source_v2=None)
    values = (
        "request-hash",
        "{}",
        "plan-hash",
        "2026-08-12T00:00:00Z",
        "2026-08-12T00:00:00Z",
    )
    with sqlite3.connect(path) as database:
        for state in ("aborting", "aborted"):
            database.execute(
                'INSERT INTO "transaction" '
                "(id, operation_id, request_hash, state, preconditions_json, "
                "plan_hash, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (state, f"operation-{state}", values[0], state, *values[1:]),
            )
        with pytest.raises(sqlite3.IntegrityError):
            database.execute(
                'INSERT INTO "transaction" '
                "(id, operation_id, request_hash, state, preconditions_json, "
                "plan_hash, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("unknown", "operation-unknown", values[0], "unknown", *values[1:]),
            )


def test_coordinator_candidate_does_not_switch_active_markdown_path(
    vault: Path, state_root: Path
) -> None:
    active = MarkdownCoordinator(vault, state_root)
    candidate = state_root / "run" / "markdown-transactions-v3.candidate.sqlite3"

    markdown_transaction.initialize_coordinator_v3_candidate(candidate, source_v2=None)
    reopened = MarkdownCoordinator(vault, state_root)

    assert active.database_path == state_root / "run" / "markdown-transactions.sqlite3"
    assert reopened.database_path == active.database_path
    assert candidate.is_file()


def test_coordinator_candidate_test_seam_requires_validation(
    state_root: Path,
) -> None:
    candidate = state_root / "run" / "markdown-transactions-v3.candidate.sqlite3"
    markdown_transaction.initialize_coordinator_v3_candidate(candidate, source_v2=None)

    opened = MarkdownCoordinator._from_v3_candidate(candidate, state_root=state_root)
    with opened._connect() as database:
        assert database.execute("PRAGMA application_id").fetchone()[0] == 0x4C575433

    with sqlite3.connect(candidate) as database:
        database.execute("PRAGMA user_version=2")
    with pytest.raises(sqlite3.DatabaseError):
        MarkdownCoordinator._from_v3_candidate(candidate, state_root=state_root)


@pytest.mark.parametrize("target_content", [b"before", b"unknown"])
def test_schema_migration_validates_and_recaptures_prepared_parent_identity(
    vault: Path, state_root: Path, target_content: bytes
):
    target = vault / "knowledge/notes/page.md"
    target.write_bytes(target_content)
    run_root = state_root / "run"
    run_root.mkdir(parents=True)
    database_path = run_root / "markdown-transactions.sqlite3"
    with sqlite3.connect(database_path) as database:
        database.executescript(
            """
            CREATE TABLE "transaction" (
                id TEXT PRIMARY KEY,
                operation_id TEXT NOT NULL UNIQUE,
                request_hash TEXT NOT NULL,
                state TEXT NOT NULL,
                preconditions_json TEXT NOT NULL,
                plan_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE "operation" (
                transaction_id TEXT NOT NULL,
                position INTEGER NOT NULL,
                kind TEXT NOT NULL,
                path TEXT NOT NULL,
                before_hash TEXT NOT NULL,
                after_hash TEXT NOT NULL,
                applied INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (transaction_id, position)
            );
            """
        )
        database.execute(
            'INSERT INTO "transaction" VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            ("legacy-tx", "legacy-op", "r", "prepared", "{}", "p", "now", "now"),
        )
        database.execute(
            'INSERT INTO "operation" VALUES (?, ?, ?, ?, ?, ?, ?)',
            (
                "legacy-tx",
                0,
                "replace",
                "knowledge/notes/page.md",
                sha256_bytes(b"before"),
                sha256_bytes(b"after"),
                0,
            ),
        )
        database.commit()

    if target_content == b"unknown":
        with pytest.raises(RuntimeError, match="unknown target bytes"):
            MarkdownCoordinator(vault, state_root)
        return

    MarkdownCoordinator(vault, state_root)

    with sqlite3.connect(database_path) as database:
        identity = database.execute(
            'SELECT parent_device, parent_inode FROM "operation" '
            "WHERE transaction_id = 'legacy-tx'"
        ).fetchone()
    metadata = target.parent.stat()
    assert tuple(markdown_transaction._decode_filesystem_id(value) for value in identity) == (
        metadata.st_dev,
        metadata.st_ino,
    )
