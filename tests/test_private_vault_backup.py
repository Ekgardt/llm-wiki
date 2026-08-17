from __future__ import annotations

import contextlib
import json
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.test_reliability_v3_adoption import _vault, build_adopted_reliability_v3


def test_staged_backup_image_uses_online_databases_and_manifest(tmp_path: Path) -> None:
    import private_vault_backup as backup

    root, state_root = _vault(tmp_path)
    build_adopted_reliability_v3(root, state_root)
    note = root / "knowledge/notes/private.md"
    note.write_bytes(b"private knowledge\n")
    (root / "cache").mkdir()
    (root / "cache/disposable.bin").write_bytes(b"cache")
    (state_root / "logs").mkdir()
    (state_root / "logs/disposable.log").write_bytes(b"log")
    staging_parent = tmp_path / "staging"
    staging_parent.mkdir()

    with backup.staged_backup_image(
        root=root,
        state_root=state_root,
        staging_parent=staging_parent,
        now=datetime(2026, 8, 15, tzinfo=timezone.utc),
        deadline=time.monotonic() + 30,
    ) as image:
        with contextlib.closing(
            sqlite3.connect(state_root / "run/markdown-transactions-v3.sqlite3")
        ) as source_database:
            assert source_database.execute(
                "SELECT COUNT(*) FROM maintenance_owners"
            ).fetchone() == (0,)
        assert (image / "vault/knowledge/notes/private.md").read_bytes() == (
            b"private knowledge\n"
        )
        assert not (image / "vault/cache").exists()
        assert not (image / "state/logs").exists()
        coordinator_copy = image / "state/run/markdown-transactions-v3.sqlite3"
        queue_copy = image / "state/run/queue-v3.sqlite3"
        with contextlib.closing(sqlite3.connect(coordinator_copy)) as database:
            assert database.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert database.execute(
                "SELECT COUNT(*) FROM maintenance_owners"
            ).fetchone() == (0,)
        with contextlib.closing(sqlite3.connect(queue_copy)) as database:
            assert database.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        manifest = json.loads((image / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["schema_version"] == "private-vault-backup/v1"
        assert manifest["created_at"] == "2026-08-15T00:00:00Z"
        note_entry = next(
            item
            for item in manifest["entries"]
            if item["path"] == "vault/knowledge/notes/private.md"
        )
        assert note_entry == {
            "path": "vault/knowledge/notes/private.md",
            "kind": "file",
            "size": 18,
            "sha256": "8175e7747329a432d40fa3b96f6c323fec3101ae4bbdbc9c26d48672ca057126",
        }
        assert backup.validate_backup_image(image)["entry_count"] == len(
            manifest["entries"]
        )

    assert list(staging_parent.iterdir()) == []


def test_staged_backup_image_blocks_source_race_and_releases_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import private_vault_backup as backup

    root, state_root = _vault(tmp_path)
    build_adopted_reliability_v3(root, state_root)
    note = root / "knowledge/notes/raced.md"
    note.write_bytes(b"before\n")
    staging_parent = tmp_path / "staging"
    staging_parent.mkdir()
    copy_regular_file = backup._copy_regular_file

    def race(source: Path, destination: Path) -> None:
        copy_regular_file(source, destination)
        if source == note:
            note.write_bytes(b"after\n")

    monkeypatch.setattr(backup, "_copy_regular_file", race)

    with pytest.raises(backup.BackupError) as raised:
        with backup.staged_backup_image(
            root=root,
            state_root=state_root,
            staging_parent=staging_parent,
            deadline=time.monotonic() + 30,
        ):
            pass

    assert raised.value.code == "source_changed"
    assert list(staging_parent.iterdir()) == []
    coordinator = state_root / "run/markdown-transactions-v3.sqlite3"
    with contextlib.closing(sqlite3.connect(coordinator)) as database:
        assert database.execute(
            "SELECT COUNT(*) FROM maintenance_owners"
        ).fetchone() == (0,)


def test_staged_backup_image_requires_quiescent_canonical_owners(tmp_path: Path) -> None:
    import private_vault_backup as backup
    from operational_ownership import OwnershipRegistry

    root, state_root = _vault(tmp_path)
    build_adopted_reliability_v3(root, state_root)
    coordinator = state_root / "run/markdown-transactions-v3.sqlite3"
    registry = OwnershipRegistry._from_adopted_database(state_root, coordinator)
    owner = registry.acquire("doctor", scope="global")
    staging_parent = tmp_path / "staging"
    staging_parent.mkdir()
    try:
        with pytest.raises(backup.BackupError) as raised:
            with backup.staged_backup_image(
                root=root,
                state_root=state_root,
                staging_parent=staging_parent,
                deadline=time.monotonic() + 30,
            ):
                pass
        assert raised.value.code == "backup_requires_quiescence"
        assert list(staging_parent.iterdir()) == []
    finally:
        registry.release(owner)


def test_staged_backup_image_rejects_unknown_runtime_projection(tmp_path: Path) -> None:
    import private_vault_backup as backup

    root, state_root = _vault(tmp_path)
    build_adopted_reliability_v3(root, state_root)
    queue = state_root / "run/queue-v3.sqlite3"
    now = datetime.now(timezone.utc).isoformat()
    with contextlib.closing(sqlite3.connect(queue)) as database:
        database.execute(
            """INSERT INTO queue_ownership(
                   actor_id,domain_role,canonical_role,canonical_scope,
                   owner_token,fencing_epoch,process_id,process_start_identity,
                   acquired_at,heartbeat_at,expires_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "orphan",
                "worker",
                "queue-worker",
                "global",
                "orphan-token",
                7,
                424242,
                "missing-process:1",
                now,
                now,
                now,
            ),
        )
        database.commit()
    staging_parent = tmp_path / "staging"
    staging_parent.mkdir()

    with pytest.raises(backup.BackupError) as raised:
        with backup.staged_backup_image(
            root=root,
            state_root=state_root,
            staging_parent=staging_parent,
            deadline=time.monotonic() + 30,
        ):
            pass

    assert raised.value.code == "runtime_state_invalid"
    assert raised.value.details == ("queue_owner_projection_orphan",)
    assert list(staging_parent.iterdir()) == []


def test_backup_private_vault_runs_exact_restic_backup_and_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import private_vault_backup as backup

    root, state_root = _vault(tmp_path)
    build_adopted_reliability_v3(root, state_root)
    (root / "knowledge/notes/private.md").write_bytes(b"private\n")
    staging = tmp_path / "staging"
    staging.mkdir()
    restic = tmp_path / "restic.exe"
    restic.write_bytes(b"fixture")
    repository_file = tmp_path / "repository.txt"
    repository_file.write_text("s3:safe-bucket/private\n", encoding="utf-8")
    calls = []

    def run(command, *, cwd, deadline, max_output_bytes=1024 * 1024):
        calls.append((command, cwd, deadline, max_output_bytes))
        if command[-1] == "version":
            return backup.CommandResult(
                0,
                b"restic 0.19.1 compiled with go1.25.1 on windows/amd64\n",
                b"",
            )
        if "backup" in command:
            assert backup.validate_backup_image(cwd)["database_count"] == 2
            with contextlib.closing(
                sqlite3.connect(state_root / "run/markdown-transactions-v3.sqlite3")
            ) as database:
                assert database.execute(
                    "SELECT COUNT(*) FROM maintenance_owners"
                ).fetchone() == (0,)
            return backup.CommandResult(
                0,
                (
                    b'{"message_type":"status","percent_done":1}\n'
                    + b'{"message_type":"summary","snapshot_id":"'
                    + b"a" * 64
                    + b'"}\n'
                ),
                b"",
            )
        assert "check" in command
        return backup.CommandResult(0, b"no errors were found\n", b"")

    monkeypatch.setattr(backup, "_run_bounded", run)

    receipt = backup.backup_private_vault(
        root=root,
        state_root=state_root,
        staging_parent=staging,
        restic_binary=restic,
        repository_file=repository_file,
        now=datetime(2026, 8, 15, tzinfo=timezone.utc),
        deadline=time.monotonic() + 30,
    )

    assert receipt == {
        "schema_version": "private-vault-backup-receipt/v1",
        "snapshot_id": "a" * 64,
        "created_at": "2026-08-15T00:00:00Z",
        "manifest_sha256": receipt["manifest_sha256"],
    }
    assert len(receipt["manifest_sha256"]) == 64
    assert calls[0][0][-1] == "version"
    assert calls[2][0][-1] == "check"
    backup_command = calls[1][0]
    assert "backup" in backup_command
    assert backup_command[-1] == "."
    assert "--json" in backup_command
    assert "llm-wiki-private-v1" in backup_command
    assert calls[1][1] == staging
    assert list(staging.iterdir()) == []


def test_backup_private_vault_rejects_restic_incomplete_exit_and_cleans_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import private_vault_backup as backup

    root, state_root = _vault(tmp_path)
    build_adopted_reliability_v3(root, state_root)
    staging = tmp_path / "staging"
    staging.mkdir()
    restic = tmp_path / "restic.exe"
    restic.write_bytes(b"fixture")
    repository_file = tmp_path / "repository.txt"
    repository_file.write_text("s3:safe-bucket/private\n", encoding="utf-8")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[-1] == "version":
            return backup.CommandResult(0, b"restic 0.19.1 fixture\n", b"")
        return backup.CommandResult(3, b"", b"source unreadable")

    monkeypatch.setattr(backup, "_run_bounded", run)

    with pytest.raises(backup.BackupError) as raised:
        backup.backup_private_vault(
            root=root,
            state_root=state_root,
            staging_parent=staging,
            restic_binary=restic,
            repository_file=repository_file,
            deadline=time.monotonic() + 30,
        )

    assert raised.value.code == "restic_backup_incomplete"
    assert len(calls) == 2
    assert list(staging.iterdir()) == []


def test_backup_private_vault_rejects_wrong_restic_version_before_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import private_vault_backup as backup

    root, state_root = _vault(tmp_path)
    build_adopted_reliability_v3(root, state_root)
    staging = tmp_path / "staging"
    staging.mkdir()
    restic = tmp_path / "restic.exe"
    restic.write_bytes(b"fixture")
    repository_file = tmp_path / "repository.txt"
    repository_file.write_text("s3:safe-bucket/private\n", encoding="utf-8")
    monkeypatch.setattr(
        backup,
        "_run_bounded",
        lambda *args, **kwargs: backup.CommandResult(
            0, b"restic 0.20.0 compiled with go1.26\n", b""
        ),
    )

    with pytest.raises(backup.BackupError) as raised:
        backup.backup_private_vault(
            root=root,
            state_root=state_root,
            staging_parent=staging,
            restic_binary=restic,
            repository_file=repository_file,
            deadline=time.monotonic() + 30,
        )

    assert raised.value.code == "restic_version_mismatch"
    assert list(staging.iterdir()) == []


def test_backup_private_vault_rejects_local_repository_inside_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import private_vault_backup as backup

    root, state_root = _vault(tmp_path)
    build_adopted_reliability_v3(root, state_root)
    staging = tmp_path / "staging"
    staging.mkdir()
    restic = tmp_path / "restic.exe"
    restic.write_bytes(b"fixture")
    repository_file = tmp_path / "repository.txt"
    repository_file.write_text(
        str(root / "restic-repository") + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        backup,
        "_run_bounded",
        lambda *args, **kwargs: pytest.fail("restic must not run"),
    )

    with pytest.raises(backup.BackupError) as raised:
        backup.backup_private_vault(
            root=root,
            state_root=state_root,
            staging_parent=staging,
            restic_binary=restic,
            repository_file=repository_file,
            deadline=time.monotonic() + 30,
        )

    assert raised.value.code == "repository_overlaps_source"
    assert list(staging.iterdir()) == []


def test_bounded_runner_kills_output_overflow() -> None:
    import private_vault_backup as backup

    with pytest.raises(backup.BackupError) as raised:
        backup._run_bounded(
            [sys.executable, "-c", "import sys; sys.stdout.write('x' * 4096)"],
            cwd=Path.cwd(),
            deadline=time.monotonic() + 30,
            max_output_bytes=1024,
        )

    assert raised.value.code == "restic_output_limit"


def _restic_restore_fixture(
    tmp_path: Path,
) -> tuple[Path, Path, Path, Path, str, str]:
    import private_vault_backup as backup

    root, state_root = _vault(tmp_path)
    build_adopted_reliability_v3(root, state_root)
    (root / "knowledge/notes/private.md").write_bytes(b"restored private\n")
    backup_staging = tmp_path / "backup-staging"
    backup_staging.mkdir()
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    with backup.staged_backup_image(
        root=root,
        state_root=state_root,
        staging_parent=backup_staging,
        now=datetime(2026, 8, 15, tzinfo=timezone.utc),
        deadline=time.monotonic() + 30,
    ) as image:
        shutil.copytree(image, snapshot, dirs_exist_ok=True, symlinks=True)
    manifest_sha256 = backup.sha256_bytes((snapshot / "manifest.json").read_bytes())
    restic = tmp_path / "restic.exe"
    restic.write_bytes(b"fixture")
    repository_file = tmp_path / "repository.txt"
    repository_file.write_text("s3:safe-bucket/private\n", encoding="utf-8")
    return root, state_root, snapshot, restic, repository_file, manifest_sha256


def test_restore_private_vault_restores_exact_snapshot_to_clean_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import private_vault_backup as backup

    _root, _state_root, snapshot, restic, repository_file, manifest_sha256 = (
        _restic_restore_fixture(tmp_path)
    )
    target = tmp_path / "restore"
    target.mkdir()
    snapshot_id = "b" * 64
    calls = []

    def run(command, *, cwd, deadline, max_output_bytes=1024 * 1024):
        calls.append((command, cwd, deadline, max_output_bytes))
        if command[-1] == "version":
            return backup.CommandResult(0, b"restic 0.19.1 fixture\n", b"")
        if command[-1] == "check":
            return backup.CommandResult(0, b"no errors were found\n", b"")
        assert "restore" in command
        assert command[command.index("restore") + 1] == snapshot_id
        assert command[command.index("--target") + 1] == str(target)
        shutil.copytree(snapshot, target, dirs_exist_ok=True, symlinks=True)
        return backup.CommandResult(
            0,
            b'{"message_type":"summary","total_files":7,'
            b'"files_restored":7,"files_skipped":0,"files_deleted":0,'
            b'"total_bytes":42,"bytes_restored":42,"bytes_skipped":0}\n',
            b"",
        )

    monkeypatch.setattr(backup, "_run_bounded", run)

    receipt = backup.restore_private_vault(
        target=target,
        restic_binary=restic,
        repository_file=repository_file,
        snapshot_id=snapshot_id,
        expected_manifest_sha256=manifest_sha256,
        deadline=time.monotonic() + 30,
    )

    assert receipt == {
        "schema_version": "private-vault-restore-receipt/v1",
        "snapshot_id": snapshot_id,
        "created_at": "2026-08-15T00:00:00Z",
        "manifest_sha256": manifest_sha256,
        "entry_count": receipt["entry_count"],
        "database_count": 2,
    }
    assert isinstance(receipt["entry_count"], int)
    assert (target / "vault/knowledge/notes/private.md").read_bytes() == (
        b"restored private\n"
    )
    assert [call[0][-1] for call in calls[:2]] == ["version", "check"]


def test_restore_private_vault_rejects_tampered_content_and_cleans_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import private_vault_backup as backup

    _root, _state_root, snapshot, restic, repository_file, manifest_sha256 = (
        _restic_restore_fixture(tmp_path)
    )
    target = tmp_path / "restore"
    target.mkdir()

    def run(command, **kwargs):
        if command[-1] == "version":
            return backup.CommandResult(0, b"restic 0.19.1 fixture\n", b"")
        if command[-1] == "check":
            return backup.CommandResult(0, b"no errors were found\n", b"")
        shutil.copytree(snapshot, target, dirs_exist_ok=True, symlinks=True)
        (target / "vault/knowledge/notes/private.md").write_bytes(b"tampered\n")
        return backup.CommandResult(
            0,
            b'{"message_type":"summary","total_files":1,'
            b'"files_restored":1,"files_skipped":0,"files_deleted":0,'
            b'"total_bytes":9,"bytes_restored":9,"bytes_skipped":0}\n',
            b"",
        )

    monkeypatch.setattr(backup, "_run_bounded", run)

    with pytest.raises(backup.BackupError) as raised:
        backup.restore_private_vault(
            target=target,
            restic_binary=restic,
            repository_file=repository_file,
            snapshot_id="c" * 64,
            expected_manifest_sha256=manifest_sha256,
            deadline=time.monotonic() + 30,
        )

    assert raised.value.code == "manifest_content_mismatch"
    assert list(target.iterdir()) == []


def test_restore_private_vault_rejects_wrong_manifest_digest_and_cleans_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import private_vault_backup as backup

    _root, _state_root, snapshot, restic, repository_file, _manifest_sha256 = (
        _restic_restore_fixture(tmp_path)
    )
    target = tmp_path / "restore"
    target.mkdir()

    def run(command, **kwargs):
        if command[-1] == "version":
            return backup.CommandResult(0, b"restic 0.19.1 fixture\n", b"")
        if command[-1] == "check":
            return backup.CommandResult(0, b"no errors were found\n", b"")
        shutil.copytree(snapshot, target, dirs_exist_ok=True, symlinks=True)
        return backup.CommandResult(
            0,
            b'{"message_type":"summary","total_files":1,'
            b'"files_restored":1,"files_skipped":0,"files_deleted":0,'
            b'"total_bytes":1,"bytes_restored":1,"bytes_skipped":0}\n',
            b"",
        )

    monkeypatch.setattr(backup, "_run_bounded", run)

    with pytest.raises(backup.BackupError) as raised:
        backup.restore_private_vault(
            target=target,
            restic_binary=restic,
            repository_file=repository_file,
            snapshot_id="d" * 64,
            expected_manifest_sha256="0" * 64,
            deadline=time.monotonic() + 30,
        )

    assert raised.value.code == "restore_manifest_mismatch"
    assert list(target.iterdir()) == []


def test_restore_private_vault_requires_empty_target_before_restic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import private_vault_backup as backup

    _root, _state_root, _snapshot, restic, repository_file, manifest_sha256 = (
        _restic_restore_fixture(tmp_path)
    )
    target = tmp_path / "restore"
    target.mkdir()
    (target / "existing.txt").write_text("keep", encoding="utf-8")
    monkeypatch.setattr(
        backup,
        "_run_bounded",
        lambda *args, **kwargs: pytest.fail("restic must not run"),
    )

    with pytest.raises(backup.BackupError) as raised:
        backup.restore_private_vault(
            target=target,
            restic_binary=restic,
            repository_file=repository_file,
            snapshot_id="e" * 64,
            expected_manifest_sha256=manifest_sha256,
            deadline=time.monotonic() + 30,
        )

    assert raised.value.code == "restore_target_not_empty"
    assert (target / "existing.txt").read_text(encoding="utf-8") == "keep"


def test_cli_backup_uses_runtime_roots_and_prints_canonical_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import private_vault_backup as backup

    root = tmp_path / "vault"
    state_root = tmp_path / "state"
    staging = tmp_path / "staging"
    restic = tmp_path / "restic.exe"
    repository_file = tmp_path / "repository.txt"
    for directory in (root, state_root, staging):
        directory.mkdir()
    restic.write_bytes(b"fixture")
    repository_file.write_text("s3:safe-bucket/private\n", encoding="utf-8")
    monkeypatch.setenv("LLM_WIKI_ROOT", str(root))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))
    captured = {}

    def run(**kwargs):
        captured.update(kwargs)
        return {
            "schema_version": "private-vault-backup-receipt/v1",
            "snapshot_id": "a" * 64,
            "created_at": "2026-08-15T00:00:00Z",
            "manifest_sha256": "b" * 64,
        }

    monkeypatch.setattr(backup, "backup_private_vault", run)

    result = backup.main(
        [
            "backup",
            "--staging",
            str(staging),
            "--restic-binary",
            str(restic),
            "--repository-file",
            str(repository_file),
            "--timeout-seconds",
            "30",
        ]
    )

    assert result == 0
    assert json.loads(capsys.readouterr().out) == run()
    assert captured["root"] == root.resolve()
    assert captured["state_root"] == state_root.resolve()
    assert captured["staging_parent"] == staging
    assert captured["deadline"] > time.monotonic()


def test_cli_restore_reports_stable_error_without_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import private_vault_backup as backup

    root = tmp_path / "vault-secret-name"
    root.mkdir()
    monkeypatch.setenv("LLM_WIKI_ROOT", str(root))
    monkeypatch.delenv("LLM_WIKI_STATE_ROOT", raising=False)

    def fail(**_kwargs):
        raise backup.BackupError("restore_runtime_invalid", ("owner_unknown",))

    monkeypatch.setattr(backup, "restore_private_vault", fail)

    result = backup.main(
        [
            "restore",
            "--target",
            str(tmp_path / "restore"),
            "--restic-binary",
            str(tmp_path / "restic.exe"),
            "--repository-file",
            str(tmp_path / "repository.txt"),
            "--snapshot-id",
            "c" * 64,
            "--manifest-sha256",
            "d" * 64,
        ]
    )

    assert result == 2
    output = capsys.readouterr().out
    assert json.loads(output) == {
        "ok": False,
        "code": "restore_runtime_invalid",
        "details": ["owner_unknown"],
    }
    assert str(tmp_path) not in output
