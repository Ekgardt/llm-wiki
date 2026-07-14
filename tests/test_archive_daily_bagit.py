from __future__ import annotations

import errno
import json
import os
import random
import shutil
import sqlite3
import stat
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from markdown_transaction import MarkdownChange, MarkdownCoordinator  # noqa: E402
from reliable_memory import sha256_bytes  # noqa: E402


class _LockedClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
        self.lock = threading.Lock()

    def __call__(self) -> datetime:
        with self.lock:
            return self.value

    def advance(self, seconds: float) -> None:
        with self.lock:
            self.value += timedelta(seconds=seconds)


@pytest.fixture
def archive_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    root = tmp_path / "vault"
    state_root = tmp_path / "state"
    state_root.mkdir()
    for relative in ("knowledge/daily/receipts", "knowledge/notes"):
        (root / relative).mkdir(parents=True)
    (root / "knowledge/index.md").write_bytes(b"# Knowledge Index\n")
    (root / "knowledge/log.md").write_bytes(b"# Session Memory Log\n")
    (root / "AGENTS.md").write_bytes(b"contract\n")
    daily = root / "knowledge/daily/2026-01-01.md"
    daily.write_bytes(b"# day\n## [evt-1] event\ncompiled evidence\n")

    import compile_memory

    monkeypatch.setattr(compile_memory, "ROOT", root)
    monkeypatch.setattr(compile_memory, "STATE_ROOT", state_root)
    monkeypatch.setattr(compile_memory, "MEMORY", root / "knowledge")
    monkeypatch.setattr(compile_memory, "DAILY_DIR", root / "knowledge/daily")
    monkeypatch.setattr(compile_memory, "KNOWLEDGE", root / "knowledge/notes")
    monkeypatch.setattr(compile_memory, "INDEX", root / "knowledge/index.md")
    monkeypatch.setattr(compile_memory, "LOG", root / "knowledge/log.md")
    monkeypatch.setattr(compile_memory, "AGENTS", root / "AGENTS.md")
    inputs = compile_memory.snapshot_compile_inputs([daily])
    compile_memory.apply_compile_plan(
        inputs,
        {"schema_version": "compile-plan/v2", "operations": []},
        action_key="b" * 64,
        trigger="manual",
        coordinator=MarkdownCoordinator(root, state_root),
        completed_at="2026-07-01T00:00:00Z",
    )
    with sqlite3.connect(state_root / "run/markdown-transactions.sqlite3") as connection:
        connection.execute(
            'UPDATE "transaction" SET updated_at="2026-05-01T00:00:00Z" '
            'WHERE operation_id LIKE "compile:%"'
        )
    return root, state_root, daily


def _archiver(root: Path, state_root: Path, **kwargs):
    from archive_daily import DailyArchiver

    return DailyArchiver(
        root,
        state_root,
        clock=lambda: datetime(2026, 7, 14, 12, tzinfo=timezone.utc),
        **kwargs,
    )


def test_eligibility_requires_age_and_exact_authoritative_v2_receipt(archive_vault) -> None:
    root, state_root, daily = archive_vault
    archiver = _archiver(root, state_root)
    assert archiver.eligible(daily, hot_days=90).eligible
    assert not archiver.eligible(daily, hot_days=365).eligible

    daily.write_bytes(daily.read_bytes() + b"uncompiled append\n")
    result = archiver.eligible(daily, hot_days=90)
    assert not result.eligible
    assert "compile_receipt" in result.reasons


def test_eligibility_rejects_nonterminal_compile_operation(archive_vault) -> None:
    root, state_root, daily = archive_vault
    digest = sha256_bytes(daily.read_bytes())
    receipt = root / f"knowledge/daily/receipts/{digest}.md"
    record = json.loads(
        receipt.read_text(encoding="utf-8").split("```json\n", 1)[1].split("\n```", 1)[0]
    )
    database = state_root / "run/markdown-transactions.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            'UPDATE "transaction" SET state="applying" WHERE operation_id=?',
            (record["operation_id"],),
        )

    result = _archiver(root, state_root).eligible(daily, hot_days=90)

    assert not result.eligible
    assert "nonterminal_compile_operation" in result.reasons


@pytest.mark.parametrize(
    ("blocker", "reason"),
    [
        ("manual_pin", "manual_pin"),
        ("decision", "decision_evidence"),
        ("queue", "queue_reference"),
        ("legacy_queue", "legacy_queue_reference"),
        ("transaction", "active_transaction"),
    ],
)
def test_eligibility_rejects_every_live_or_pinned_reference(
    archive_vault, blocker: str, reason: str
) -> None:
    root, state_root, daily = archive_vault
    digest = sha256_bytes(daily.read_bytes())
    coordinator = MarkdownCoordinator(root, state_root)

    if blocker == "manual_pin":
        (state_root / "run" / "archive-pins.json").write_text(
            json.dumps({"daily_ids": [daily.stem], "source_hashes": []}), encoding="utf-8"
        )
    elif blocker == "decision":
        (root / "knowledge/notes/decision.md").write_text(
            f"---\ntype: decision\n---\n\nEvidence: daily:{daily.stem} sha256:{digest}\n",
            encoding="utf-8",
        )
    elif blocker == "queue":
        from memory_queue import MemoryQueue

        MemoryQueue(state_root).enqueue("compile", 1, {"daily_id": daily.stem, "hash": digest})
    elif blocker == "legacy_queue":
        legacy = state_root / "run" / "queue"
        legacy.mkdir()
        (legacy / "task.json").write_text(json.dumps({"daily_id": daily.stem}), encoding="utf-8")
    elif blocker == "transaction":
        coordinator.prepare(
            [MarkdownChange.delete(f"knowledge/daily/{daily.name}", max_before_bytes=1024)],
            operation_id="pending-daily-delete",
            preconditions={f"knowledge/daily/{daily.name}": digest},
        )
    result = _archiver(root, state_root).eligible(daily, hot_days=90)
    assert not result.eligible
    assert reason in result.reasons


def test_eligibility_is_bounded_by_active_writer(archive_vault) -> None:
    root, state_root, daily = archive_vault
    coordinator = MarkdownCoordinator(root, state_root)
    with coordinator.writer_gate():
        with pytest.raises(TimeoutError, match="writer gate"):
            _archiver(root, state_root).eligible(daily, hot_days=90)


def test_recent_compile_receipt_transaction_pins_source_for_undo(archive_vault) -> None:
    root, state_root, daily = archive_vault
    with sqlite3.connect(state_root / "run/markdown-transactions.sqlite3") as connection:
        connection.execute(
            'UPDATE "transaction" SET updated_at="2026-07-14T11:00:00Z" '
            'WHERE operation_id LIKE "compile:%"'
        )

    result = _archiver(root, state_root).eligible(
        daily, hot_days=90, transaction_retention_days=30
    )

    assert not result.eligible
    assert "transaction_retention" in result.reasons


def test_dead_queue_reference_pins_source(archive_vault) -> None:
    from memory_queue import MemoryQueue

    root, state_root, daily = archive_vault
    digest = sha256_bytes(daily.read_bytes())
    queue = MemoryQueue(state_root)
    task_id = queue.enqueue("compile", 1, {"daily_id": daily.stem, "digest": digest})
    with sqlite3.connect(queue.db_path) as connection:
        connection.execute("UPDATE tasks SET state='dead' WHERE id=?", (task_id,))

    result = _archiver(root, state_root).eligible(daily, hot_days=90)

    assert not result.eligible
    assert "queue_reference" in result.reasons


def test_durable_source_failure_pins_archive_under_source_fence(archive_vault) -> None:
    from memory_queue import MemoryQueue

    root, state_root, daily = archive_vault
    digest = sha256_bytes(daily.read_bytes())
    queue = MemoryQueue(state_root)
    queue.record_source_failure(
        f"knowledge/daily/{daily.name}",
        digest,
        error_code="compile_failed",
        producer="compile",
    )

    result = _archiver(root, state_root).eligible(daily, hot_days=90)
    assert not result.eligible
    assert "source_failure" in result.reasons

    with pytest.raises(ValueError, match="source_failure"):
        _archiver(root, state_root).archive(daily.stem)
    assert daily.exists()


def test_archive_publishes_complete_valid_bag_then_transactionally_removes_source(
    archive_vault,
) -> None:
    from evidence_resolver import EvidenceRef, EvidenceResolver, validate_bag

    root, state_root, daily = archive_vault
    source = daily.read_bytes()
    digest = sha256_bytes(source)
    receipt = _archiver(root, state_root).archive(daily.stem)

    assert receipt.state == "archived"
    assert not daily.exists()
    bag = validate_bag(
        receipt.bag_path,
        coordinator=MarkdownCoordinator(root, state_root),
        vault=root,
    )
    assert bag.manifest["logical_daily_id"] == daily.stem
    assert bag.manifest["source_hash"] == digest
    assert bag.manifest["payload_hash"] == digest
    assert bag.manifest["queue_preflight"]["passed"] is True
    assert bag.manifest["retention_days"] == 90
    assert bag.manifest["operations"][0]["state"] == "succeeded"
    assert (receipt.bag_path / "bag-info.txt").read_bytes() == (
        b"Bagging-Date: 2026-07-14\n"
        + f"Payload-Oxum: {len(source)}.1\n".encode()
        + b"External-Identifier: daily:2026-01-01\n"
    )
    assert not list(receipt.bag_path.parent.glob(".*.building-*"))
    assert not list(receipt.bag_path.rglob("*.gz"))
    if os.name == "posix":
        assert stat.S_IMODE(receipt.bag_path.stat().st_mode) == 0o500
        assert stat.S_IMODE(bag.payload_path.stat().st_mode) == 0o400

    evidence = bag.manifest["evidence"][0]
    ref = EvidenceRef(
        daily.stem,
        digest,
        evidence["block_id"],
        evidence["byte_start"],
        evidence["byte_end"],
    )
    assert EvidenceResolver(root, state_root=state_root).resolve(ref).bytes == source[
        evidence["byte_start"] :
    ]
    coordinator = MarkdownCoordinator(root, state_root)
    removal = coordinator._record_for_operation_id(
        f"archive-remove:{daily.stem}:{digest}"
    )
    assert removal is not None and removal.state == "committed"


def test_archived_evidence_resolves_after_eligible_run_state_is_deleted(
    archive_vault,
) -> None:
    from evidence_resolver import EvidenceRef, EvidenceResolver

    root, state_root, daily = archive_vault
    archived = _archiver(root, state_root).archive(daily.stem)
    manifest = json.loads((archived.bag_path / "archive-manifest.json").read_bytes())
    embedded_receipt = archived.bag_path / "compile-receipt.md"
    assert embedded_receipt.read_bytes() == root.joinpath(
        f"knowledge/daily/receipts/{archived.source_sha256}.md"
    ).read_bytes()
    assert set(manifest["compile_authority"]) == {
        "commit_sequence",
        "committed_at",
        "coordinator_record",
        "coordinator_record_digest",
        "operation_ids",
        "schema",
        "state",
        "transaction_id",
    }
    evidence = manifest["evidence"][0]
    ref = EvidenceRef(
        daily.stem,
        archived.source_sha256,
        evidence["block_id"],
        evidence["byte_start"],
        evidence["byte_end"],
    )
    shutil.rmtree(state_root / "run")

    resolved = EvidenceResolver(root, state_root=state_root).resolve(ref)

    assert resolved.location == "archive"
    assert resolved.bytes == archived.bag_path.joinpath(
        f"data/{daily.name}"
    ).read_bytes()[evidence["byte_start"] : evidence["byte_end"]]


def test_forged_embedded_receipt_fails_after_outer_hashes_are_rebuilt(
    archive_vault,
) -> None:
    from archive_daily import DailyArchiver
    from evidence_resolver import EvidenceResolutionError, validate_bag
    from reliable_memory import canonical_json_bytes

    root, state_root, daily = archive_vault
    archived = _archiver(root, state_root).archive(daily.stem)
    forged = root / "forged-self-contained-bag"
    shutil.copytree(archived.bag_path, forged)
    for path in (forged, *forged.rglob("*")):
        path.chmod(0o700 if path.is_dir() else 0o600)
    embedded = forged / "compile-receipt.md"
    embedded.write_bytes(embedded.read_bytes().replace(b'"state":"completed"', b'"state":"forged"'))
    manifest_path = forged / "archive-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["compile_receipt_ref"]["receipt_file_hash"] = sha256_bytes(
        embedded.read_bytes()
    )
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    tags = (
        "archive-manifest.json",
        "bag-info.txt",
        "bagit.txt",
        "compile-receipt.md",
        "manifest-sha256.txt",
    )
    (forged / "tagmanifest-sha256.txt").write_bytes(
        "".join(
            f"{sha256_bytes((forged / name).read_bytes())}  {name}\n" for name in tags
        ).encode()
    )
    DailyArchiver._seal(forged)
    shutil.rmtree(state_root / "run")

    with pytest.raises(EvidenceResolutionError, match="compile receipt"):
        validate_bag(forged, vault=root)


def test_forged_offline_coordinator_record_fails_its_attested_digest(
    archive_vault,
) -> None:
    from archive_daily import DailyArchiver
    from evidence_resolver import EvidenceResolutionError, validate_bag
    from reliable_memory import canonical_json_bytes

    root, state_root, daily = archive_vault
    archived = _archiver(root, state_root).archive(daily.stem)
    forged = root / "forged-coordinator-record-bag"
    shutil.copytree(archived.bag_path, forged)
    for path in (forged, *forged.rglob("*")):
        path.chmod(0o700 if path.is_dir() else 0o600)
    manifest_path = forged / "archive-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["compile_authority"]["coordinator_record"]["updated_at"] = (
        "2026-01-01T00:00:00Z"
    )
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    tags = (
        "archive-manifest.json",
        "bag-info.txt",
        "bagit.txt",
        "compile-receipt.md",
        "manifest-sha256.txt",
    )
    (forged / "tagmanifest-sha256.txt").write_bytes(
        "".join(
            f"{sha256_bytes((forged / name).read_bytes())}  {name}\n" for name in tags
        ).encode()
    )
    DailyArchiver._seal(forged)
    shutil.rmtree(state_root / "run")

    with pytest.raises(EvidenceResolutionError, match="authority"):
        validate_bag(forged, vault=root)


def test_bag_validation_rechecks_receipt_hash_and_transaction_authority(
    archive_vault,
) -> None:
    from evidence_resolver import EvidenceResolutionError, validate_bag

    root, state_root, daily = archive_vault
    receipt = _archiver(root, state_root).archive(daily.stem)
    coordinator = MarkdownCoordinator(root, state_root)
    copy = root / "receipt-authority-copy"
    shutil.copytree(receipt.bag_path, copy)
    for path in (copy, *copy.rglob("*")):
        path.chmod(0o700 if path.is_dir() else 0o600)
    manifest_path = copy / "archive-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["compile_authority"]["commit_sequence"] += 1
    from reliable_memory import canonical_json_bytes

    manifest_path.write_bytes(canonical_json_bytes(manifest))
    tags = (
        "archive-manifest.json",
        "bag-info.txt",
        "bagit.txt",
        "compile-receipt.md",
        "manifest-sha256.txt",
    )
    (copy / "tagmanifest-sha256.txt").write_bytes(
        "".join(
            f"{sha256_bytes((copy / name).read_bytes())}  {name}\n" for name in tags
        ).encode()
    )
    from archive_daily import DailyArchiver

    DailyArchiver._seal(copy)

    with pytest.raises(EvidenceResolutionError, match="coordinator"):
        validate_bag(copy, coordinator=coordinator, vault=root)


def test_bag_info_requires_exact_fields_order_and_no_duplicates(archive_vault) -> None:
    from archive_daily import DailyArchiver
    from evidence_resolver import EvidenceResolutionError, validate_bag

    root, state_root, daily = archive_vault
    archived = _archiver(root, state_root).archive(daily.stem)
    copy = root / "bag-info-copy"
    shutil.copytree(archived.bag_path, copy)
    info = copy / "bag-info.txt"
    info.write_bytes(info.read_bytes() + b"External-Identifier: daily:2026-01-01\n")
    tags = (
        "archive-manifest.json",
        "bag-info.txt",
        "bagit.txt",
        "compile-receipt.md",
        "manifest-sha256.txt",
    )
    (copy / "tagmanifest-sha256.txt").write_bytes(
        "".join(
            f"{sha256_bytes((copy / name).read_bytes())}  {name}\n" for name in tags
        ).encode()
    )
    DailyArchiver._seal(copy)

    with pytest.raises(EvidenceResolutionError, match="bag info"):
        validate_bag(
            copy,
            coordinator=MarkdownCoordinator(root, state_root),
            vault=root,
        )


def test_bag_validation_rejects_any_writable_member(
    archive_vault, monkeypatch
) -> None:
    import evidence_resolver

    root, state_root, daily = archive_vault
    receipt = _archiver(root, state_root).archive(daily.stem)
    payload = receipt.bag_path / f"data/{daily.name}"
    original = evidence_resolver._archive_path_is_read_only
    monkeypatch.setattr(
        evidence_resolver,
        "_archive_path_is_read_only",
        lambda path: False if path == payload else original(path),
    )

    with pytest.raises(evidence_resolver.EvidenceResolutionError, match="immutable"):
        evidence_resolver.validate_bag(
            receipt.bag_path,
            coordinator=MarkdownCoordinator(root, state_root),
            vault=root,
        )


def test_archive_index_is_canonical_deterministic_and_derived(archive_vault) -> None:
    root, state_root, daily = archive_vault
    archiver = _archiver(root, state_root)
    receipt = archiver.archive(daily.stem)
    first = archiver.rebuild_index().read_bytes()
    second = archiver.rebuild_index().read_bytes()
    assert first == second
    assert json.loads(first) == {
        "schema_version": "archive-index/v1",
        "bags": [
            {
                "bag_path": receipt.bag_path.relative_to(root).as_posix(),
                "logical_daily_id": daily.stem,
                "source_hash": receipt.source_sha256,
            }
        ],
    }


def test_archiver_bounds_month_iteration_before_filtering(
    archive_vault, monkeypatch
) -> None:
    import archive_daily

    root, state_root, _daily = archive_vault
    month = root / "knowledge/daily/archive/2026-01"
    month.mkdir(parents=True)
    (month / "unrelated-one").mkdir()
    (month / "unrelated-two").mkdir()
    monkeypatch.setattr(archive_daily, "MAX_ARCHIVE_ENTRIES", 1)

    with pytest.raises(ValueError, match="entry scan limit"):
        _archiver(root, state_root)._archive_paths(hidden=False)


def test_archive_refuses_linked_archive_boundary_before_writing_outside(
    archive_vault,
) -> None:
    root, state_root, daily = archive_vault
    outside = root.parent / "outside-archive"
    outside.mkdir()
    archive_root = root / "knowledge/daily/archive"
    try:
        os.symlink(outside, archive_root, target_is_directory=True)
    except OSError:
        pytest.skip("directory links require privileges on this platform")

    with pytest.raises((PermissionError, ValueError), match="symlink|regular"):
        _archiver(root, state_root).archive(daily.stem)
    assert list(outside.iterdir()) == []


def test_archive_rejects_mocked_windows_reparse_boundary(
    archive_vault, monkeypatch
) -> None:
    root, state_root, daily = archive_vault
    archive_root = root / "knowledge/daily/archive"
    archive_root.mkdir()
    original = Path.lstat

    def reparse(path: Path):
        result = original(path)
        if path == archive_root:
            return SimpleNamespace(
                st_mode=result.st_mode,
                st_file_attributes=0x400,
            )
        return result

    monkeypatch.setattr(Path, "lstat", reparse)
    with pytest.raises(PermissionError, match="non-symlink"):
        _archiver(root, state_root).archive(daily.stem)


@pytest.mark.parametrize("killpoint", ["after_build", "before_publish_rename"])
def test_kill_before_atomic_publish_never_exposes_final_bag(
    archive_vault, killpoint: str
) -> None:
    root, state_root, daily = archive_vault

    def crash(point: str) -> None:
        if point == killpoint:
            raise RuntimeError("crash")

    with pytest.raises(RuntimeError, match="crash"):
        _archiver(root, state_root, killpoint=crash).archive(daily.stem)
    assert daily.exists()
    assert not list((root / "knowledge/daily/archive").rglob("bag-*"))


def test_hidden_build_has_canonical_intent_and_recovery_resumes_it(
    archive_vault,
) -> None:
    from reliable_memory import canonical_json_bytes

    root, state_root, daily = archive_vault
    archiver = _archiver(root, state_root)
    eligibility = archiver.eligible(daily, hot_days=90)
    final, hidden = archiver._build_bag(
        daily.stem, daily.read_bytes(), eligibility, hot_days=90
    )
    intent_path = hidden / "build-intent.json"
    intent = json.loads(intent_path.read_bytes())
    assert intent_path.read_bytes() == canonical_json_bytes(intent)
    assert intent == {
        "created_at": "2026-07-14T12:00:00Z",
        "final_bag_name": final.name,
        "logical_daily_id": daily.stem,
        "schema_version": "archive-build-intent/v1",
        "source_hash": sha256_bytes(daily.read_bytes()),
    }

    recovered = archiver.recover()

    assert [item.state for item in recovered] == ["recovered"]
    assert final.is_dir()
    assert not hidden.exists()
    assert not daily.exists()


def test_build_failure_always_cleans_owned_hidden_directory(
    archive_vault, monkeypatch
) -> None:
    import archive_daily

    root, state_root, daily = archive_vault
    monkeypatch.setattr(
        archive_daily.DailyArchiver,
        "_seal",
        staticmethod(lambda path: (_ for _ in ()).throw(PermissionError("seal failed"))),
    )

    with pytest.raises(PermissionError, match="seal failed"):
        _archiver(root, state_root).archive(daily.stem)

    month = root / "knowledge/daily/archive/2026-01"
    assert not [path for path in month.iterdir() if ".building-" in path.name]
    assert daily.exists()


def test_malicious_hidden_build_link_is_quarantined_without_touching_target(
    archive_vault,
) -> None:
    from archive_daily import ArchiveConflict
    from reliable_memory import canonical_json_bytes

    root, state_root, daily = archive_vault
    month = root / "knowledge/daily/archive/2026-01"
    month.mkdir(parents=True)
    hidden = month / ".bag-2026-01-01.building-malicious"
    hidden.mkdir()
    outside = root / "outside.txt"
    outside.write_text("untouched", encoding="utf-8")
    intent = {
        "created_at": "2026-07-14T12:00:00Z",
        "final_bag_name": "bag-malicious-2026-01-01",
        "logical_daily_id": daily.stem,
        "schema_version": "archive-build-intent/v1",
        "source_hash": sha256_bytes(daily.read_bytes()),
    }
    (hidden / "build-intent.json").write_bytes(canonical_json_bytes(intent))
    try:
        os.symlink(outside, hidden / "payload-link")
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(ArchiveConflict):
        _archiver(root, state_root).recover()

    assert outside.read_text(encoding="utf-8") == "untouched"
    assert not hidden.exists()
    assert any(
        path.name.startswith(hidden.name)
        for path in (state_root / "run/archive-quarantine/builds").iterdir()
    )


def test_mocked_reparse_hidden_member_is_quarantined_without_acl_follow(
    archive_vault, monkeypatch
) -> None:
    from archive_daily import ArchiveConflict
    from reliable_memory import canonical_json_bytes

    root, state_root, daily = archive_vault
    month = root / "knowledge/daily/archive/2026-01"
    month.mkdir(parents=True)
    hidden = month / ".bag-2026-01-01.building-reparse"
    hidden.mkdir()
    intent = {
        "created_at": "2026-07-14T12:00:00Z",
        "final_bag_name": "bag-malicious-2026-01-01",
        "logical_daily_id": daily.stem,
        "schema_version": "archive-build-intent/v1",
        "source_hash": sha256_bytes(daily.read_bytes()),
    }
    (hidden / "build-intent.json").write_bytes(canonical_json_bytes(intent))
    suspect = hidden / "suspect"
    suspect.write_bytes(b"do not follow")
    original_lstat = Path.lstat

    def reparse(path: Path):
        info = original_lstat(path)
        if path == suspect:
            return SimpleNamespace(
                st_mode=info.st_mode,
                st_file_attributes=0x400,
                st_size=info.st_size,
            )
        return info

    monkeypatch.setattr(Path, "lstat", reparse)
    with pytest.raises(ArchiveConflict):
        _archiver(root, state_root).recover()
    assert not hidden.exists()


def test_cross_volume_quarantine_uses_verified_copy_then_removes_active_bag(
    archive_vault, monkeypatch
) -> None:
    root, state_root, daily = archive_vault

    def crash(point: str) -> None:
        if point == "after_publish_rename":
            raise RuntimeError("crash")

    with pytest.raises(RuntimeError):
        _archiver(root, state_root, killpoint=crash).archive(daily.stem)
    bag = next(
        path for path in (root / "knowledge/daily/archive").rglob("bag-*") if path.is_dir()
    )
    original_payload = (bag / f"data/{daily.name}").read_bytes()
    daily.write_bytes(daily.read_bytes() + b"different\n")
    original_replace = Path.replace

    def cross_volume(path: Path, target: Path):
        if path == bag and "archive-quarantine" in str(target):
            raise OSError(errno.EXDEV, "cross-device link")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", cross_volume)
    recovered = _archiver(root, state_root).recover()

    assert [item.state for item in recovered] == ["quarantined"]
    assert not bag.exists()
    copies = list((state_root / "run/archive-quarantine/bags").iterdir())
    assert len(copies) == 1
    assert (copies[0] / f"data/{daily.name}").read_bytes() == original_payload


def test_windows_sharing_failure_keeps_source_and_releases_queue_fence(
    archive_vault, monkeypatch
) -> None:
    from memory_queue import MemoryQueue

    root, state_root, daily = archive_vault
    original_replace = Path.replace

    def sharing_failure(path: Path, target: Path):
        if ".building-" in path.name:
            raise PermissionError(32, "sharing violation", str(path))
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", sharing_failure)
    with pytest.raises(PermissionError, match="sharing violation"):
        _archiver(root, state_root).archive(daily.stem)
    assert daily.exists()
    assert MemoryQueue(state_root).enqueue("compile", 1, {"daily_id": daily.stem})


def test_recovery_finishes_identical_duplicate_after_publish_crash(archive_vault) -> None:
    root, state_root, daily = archive_vault

    def crash(point: str) -> None:
        if point == "after_publish_rename":
            raise RuntimeError("crash")

    with pytest.raises(RuntimeError, match="crash"):
        _archiver(root, state_root, killpoint=crash).archive(daily.stem)
    assert daily.exists()
    assert len(
        [path for path in (root / "knowledge/daily/archive").rglob("bag-*") if path.is_dir()]
    ) == 1

    recovered = _archiver(root, state_root).recover()
    assert [item.state for item in recovered] == ["recovered"]
    assert not daily.exists()


def test_archive_starts_with_recovery_and_reuses_exact_published_bag(
    archive_vault,
) -> None:
    root, state_root, daily = archive_vault

    def crash(point: str) -> None:
        if point == "after_publish_rename":
            raise RuntimeError("crash")

    with pytest.raises(RuntimeError):
        _archiver(root, state_root, killpoint=crash).archive(daily.stem)
    original = next(
        path for path in (root / "knowledge/daily/archive").rglob("bag-*") if path.is_dir()
    )

    result = _archiver(root, state_root).archive(daily.stem)

    assert result.bag_path == original
    assert result.state == "recovered"
    assert len(_archiver(root, state_root)._archive_paths(hidden=False)) == 1


def test_failure_created_after_publish_prevents_recovery_source_delete(
    archive_vault,
) -> None:
    from memory_queue import MemoryQueue

    root, state_root, daily = archive_vault
    digest = sha256_bytes(daily.read_bytes())

    def fail_after_publish(point: str) -> None:
        if point == "after_publish_rename":
            MemoryQueue(state_root).record_source_failure(
                f"knowledge/daily/{daily.name}",
                digest,
                error_code="late_compile_failure",
                producer="compile",
            )
            raise RuntimeError("crash after late failure")

    with pytest.raises(RuntimeError, match="late failure"):
        _archiver(root, state_root, killpoint=fail_after_publish).archive(daily.stem)
    bag = next(
        path for path in (root / "knowledge/daily/archive").rglob("bag-*") if path.is_dir()
    )

    with pytest.raises(ValueError, match="source_failure"):
        _archiver(root, state_root).recover()

    assert daily.exists()
    assert bag.exists()


def test_archive_heartbeats_source_fence_past_original_lease(
    archive_vault,
) -> None:
    from memory_queue import MemoryQueue

    root, state_root, daily = archive_vault
    clock = _LockedClock()
    four_heartbeats = threading.Event()
    waits: list[float] = []

    def wait(stop: threading.Event, interval: float) -> bool:
        waits.append(interval)
        clock.advance(interval)
        if len(waits) == 4:
            four_heartbeats.set()
            return stop.wait(5)
        return False

    queue = MemoryQueue(state_root, clock=clock, heartbeat_wait=wait)

    def hold_build(point: str) -> None:
        if point == "after_build":
            assert four_heartbeats.wait(5)

    archived = _archiver(
        root,
        state_root,
        queue=queue,
        killpoint=hold_build,
    ).archive(daily.stem)

    assert archived.state == "archived"
    assert not daily.exists()
    assert waits[:4] == [40] * 4
    assert not any(
        thread.name.startswith("memory-source-fence-heartbeat-") and thread.is_alive()
        for thread in threading.enumerate()
    )


def test_archive_stops_before_publish_when_source_heartbeat_loses_takeover(
    archive_vault, monkeypatch
) -> None:
    from memory_queue import MemoryQueue, QueueOperationError

    root, state_root, daily = archive_vault
    clock = _LockedClock()
    heartbeat_lost = threading.Event()
    replacements = []

    def wait(stop: threading.Event, interval: float) -> bool:
        del stop, interval
        clock.advance(121)
        replacement_queue = MemoryQueue(
            state_root, clock=clock, rng=random.Random(99)
        )
        replacements.append(
            replacement_queue.acquire_source_fence(
                daily.stem, sha256_bytes(daily.read_bytes())
            )
        )
        return False

    queue = MemoryQueue(state_root, clock=clock, heartbeat_wait=wait)
    original_heartbeat = queue.heartbeat_source_fence

    def observe_lost(fence, *, lease_seconds=120):
        try:
            return original_heartbeat(fence, lease_seconds=lease_seconds)
        except QueueOperationError:
            heartbeat_lost.set()
            raise

    monkeypatch.setattr(queue, "heartbeat_source_fence", observe_lost)

    def hold_build(point: str) -> None:
        if point == "after_build":
            assert heartbeat_lost.wait(5)

    with pytest.raises(RuntimeError) as raised:
        _archiver(
            root,
            state_root,
            queue=queue,
            killpoint=hold_build,
        ).archive(daily.stem)

    assert getattr(raised.value, "code", None) == "archive_source_fence_lost"
    assert replacements
    assert daily.exists()
    assert not list((root / "knowledge/daily/archive").rglob("bag-*"))
    assert not any(
        thread.name.startswith("memory-source-fence-heartbeat-") and thread.is_alive()
        for thread in threading.enumerate()
    )


def test_failure_winning_finalization_race_preserves_flat_source(
    archive_vault, monkeypatch
) -> None:
    from memory_queue import MemoryQueue

    root, state_root, daily = archive_vault
    digest = sha256_bytes(daily.read_bytes())
    archive_at_revalidate = threading.Event()
    continue_archive = threading.Event()
    failure_holds_lock = threading.Event()
    continue_failure = threading.Event()
    archive_done = threading.Event()
    errors: list[BaseException] = []
    original_record = MemoryQueue._record_source_failure_row

    def pause_record(*args) -> None:
        failure_holds_lock.set()
        assert continue_failure.wait(5)
        original_record(*args)

    monkeypatch.setattr(
        MemoryQueue, "_record_source_failure_row", staticmethod(pause_record)
    )

    def killpoint(point: str) -> None:
        if point == "after_revalidate":
            archive_at_revalidate.set()
            assert continue_archive.wait(5)

    def archive() -> None:
        try:
            _archiver(root, state_root, killpoint=killpoint).archive(daily.stem)
        except BaseException as exc:
            errors.append(exc)
        finally:
            archive_done.set()

    archive_thread = threading.Thread(target=archive)
    archive_thread.start()
    assert archive_at_revalidate.wait(10)

    failure_thread = threading.Thread(
        target=lambda: MemoryQueue(state_root).record_source_failure(
            f"knowledge/daily/{daily.name}",
            digest,
            error_code="race_failure_wins",
            producer="compile",
        )
    )
    failure_thread.start()
    assert failure_holds_lock.wait(5)
    continue_archive.set()
    archive_completed_while_failure_locked = archive_done.wait(0.25)
    continue_failure.set()
    archive_thread.join(10)
    failure_thread.join(10)

    assert not archive_thread.is_alive() and not failure_thread.is_alive()
    assert not archive_completed_while_failure_locked
    assert len(errors) == 1 and "source_failure" in str(errors[0])
    assert daily.exists()


def test_archive_winning_finalization_race_deletes_before_failure_records(
    archive_vault, monkeypatch
) -> None:
    from memory_queue import MemoryQueue

    root, state_root, daily = archive_vault
    digest = sha256_bytes(daily.read_bytes())
    archiver = _archiver(root, state_root)
    deletion_started = threading.Event()
    continue_deletion = threading.Event()
    failure_started = threading.Event()
    failure_connected = threading.Event()
    failure_done = threading.Event()
    errors: list[BaseException] = []
    original_apply = archiver.coordinator.apply
    original_connect = MemoryQueue._connect

    def pause_delete(transaction_id: str):
        record = archiver.coordinator._record(transaction_id)
        if record is not None and record.operation_id.startswith("archive-remove:"):
            deletion_started.set()
            assert continue_deletion.wait(5)
        return original_apply(transaction_id)

    monkeypatch.setattr(archiver.coordinator, "apply", pause_delete)

    def observe_failure_connection(queue: MemoryQueue):
        connection = original_connect(queue)
        if threading.current_thread().name == "archive-failure-writer":
            failure_connected.set()
        return connection

    monkeypatch.setattr(MemoryQueue, "_connect", observe_failure_connection)

    def archive() -> None:
        try:
            archiver.archive(daily.stem)
        except BaseException as exc:
            errors.append(exc)

    def record_failure() -> None:
        failure_started.set()
        MemoryQueue(state_root).record_source_failure(
            f"knowledge/daily/{daily.name}",
            digest,
            error_code="archive_won_race",
            producer="compile",
        )
        failure_done.set()

    archive_thread = threading.Thread(target=archive)
    archive_thread.start()
    assert deletion_started.wait(10)
    failure_thread = threading.Thread(
        target=record_failure, name="archive-failure-writer"
    )
    failure_thread.start()
    assert failure_started.wait(5)
    assert failure_connected.wait(5)
    failure_completed_while_delete_paused = failure_done.wait(0.25)
    continue_deletion.set()
    archive_thread.join(10)
    failure_thread.join(10)

    assert not archive_thread.is_alive() and not failure_thread.is_alive()
    assert not failure_completed_while_delete_paused
    assert errors == []
    assert not daily.exists()
    assert MemoryQueue(state_root).source_failure(
        f"knowledge/daily/{daily.name}", digest
    ) is not None


def test_mismatched_existing_bag_raises_stable_conflict_and_preserves_both_payloads(
    archive_vault,
) -> None:
    from archive_daily import ArchiveConflict

    root, state_root, daily = archive_vault

    def crash(point: str) -> None:
        if point == "after_publish_rename":
            raise RuntimeError("crash")

    with pytest.raises(RuntimeError):
        _archiver(root, state_root, killpoint=crash).archive(daily.stem)
    original = daily.read_bytes()
    daily.write_bytes(original + b"new source\n")

    with pytest.raises(ArchiveConflict) as raised:
        _archiver(root, state_root).archive(daily.stem)

    assert raised.value.code == "archive_source_conflict"
    assert daily.read_bytes() == original + b"new source\n"
    quarantined = list((state_root / "run/archive-quarantine/bags").iterdir())
    assert len(quarantined) == 1
    assert (quarantined[0] / f"data/{daily.name}").read_bytes() == original


def test_recovery_reduces_exact_duplicate_bags_to_one_active_copy(archive_vault) -> None:
    root, state_root, daily = archive_vault

    def crash(point: str) -> None:
        if point == "after_publish_rename":
            raise RuntimeError("crash")

    with pytest.raises(RuntimeError):
        _archiver(root, state_root, killpoint=crash).archive(daily.stem)
    bag = next(
        path for path in (root / "knowledge/daily/archive").rglob("bag-*") if path.is_dir()
    )
    duplicate = bag.parent / f"bag-duplicate-{daily.stem}"
    shutil.copytree(bag, duplicate)
    from archive_daily import DailyArchiver

    DailyArchiver._seal(duplicate)

    _archiver(root, state_root).archive(daily.stem)

    assert len(_archiver(root, state_root)._archive_paths(hidden=False)) == 1
    quarantined = state_root / "run/archive-quarantine/bags"
    assert any(path.is_dir() for path in quarantined.iterdir())


@pytest.mark.parametrize("killpoint", ["after_revalidate", "after_source_delete"])
def test_recovery_handles_every_post_publish_boundary(
    archive_vault, killpoint: str
) -> None:
    root, state_root, daily = archive_vault

    def crash(point: str) -> None:
        if point == killpoint:
            raise RuntimeError("crash")

    with pytest.raises(RuntimeError, match="crash"):
        _archiver(root, state_root, killpoint=crash).archive(daily.stem)
    bags = [
        path for path in (root / "knowledge/daily/archive").rglob("bag-*") if path.is_dir()
    ]
    assert len(bags) == 1

    recovered = _archiver(root, state_root).recover()
    assert not daily.exists()
    assert [item.state for item in recovered] == (
        ["recovered"] if killpoint == "after_revalidate" else []
    )
    assert (root / "knowledge/daily/archive/archive-index.json").is_file()


def test_recovery_quarantines_mismatched_duplicate_and_deletes_neither(archive_vault) -> None:
    root, state_root, daily = archive_vault

    def crash(point: str) -> None:
        if point == "after_publish_rename":
            raise RuntimeError("crash")

    with pytest.raises(RuntimeError):
        _archiver(root, state_root, killpoint=crash).archive(daily.stem)
    daily.write_bytes(daily.read_bytes() + b"different\n")
    bag = next(
        path for path in (root / "knowledge/daily/archive").rglob("bag-*") if path.is_dir()
    )

    recovered = _archiver(root, state_root).recover()
    assert [item.state for item in recovered] == ["quarantined"]
    assert daily.exists()
    assert not bag.exists()
    moved = state_root / "run/archive-quarantine/bags" / bag.name
    assert moved.is_dir()
    records = list((state_root / "run/archive-quarantine").glob("*.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_bytes())["reason"] == "duplicate_hash_mismatch"


@pytest.mark.parametrize("failure_point", ["after_publish_rename", "after_source_delete"])
def test_archive_holds_queue_source_fence_until_failure_cleanup(
    archive_vault, failure_point: str
) -> None:
    from memory_queue import MemoryQueue, QueueOperationError

    root, state_root, daily = archive_vault
    queue = MemoryQueue(state_root)

    def inspect(point: str) -> None:
        if point == failure_point:
            with pytest.raises(QueueOperationError, match="source_fenced"):
                queue.enqueue("compile", 1, {"daily_id": daily.stem})
            raise RuntimeError("crash")

    with pytest.raises(RuntimeError):
        _archiver(root, state_root, killpoint=inspect).archive(daily.stem)

    assert queue.enqueue("compile", 1, {"daily_id": daily.stem})


def test_eligible_runs_inside_writer_gate(archive_vault, monkeypatch) -> None:
    root, state_root, daily = archive_vault
    archiver = _archiver(root, state_root)
    entered = 0
    original = archiver.coordinator.writer_gate

    @contextmanager
    def observed_gate(*, wait_seconds=None):
        nonlocal entered
        entered += 1
        with original(wait_seconds=wait_seconds):
            yield

    monkeypatch.setattr(archiver.coordinator, "writer_gate", observed_gate)
    assert archiver.eligible(daily, hot_days=90).eligible
    assert entered == 1


def test_sealing_and_parent_fsync_fail_closed(archive_vault, monkeypatch) -> None:
    import archive_daily

    root, state_root, daily = archive_vault
    synced: list[Path] = []
    original_sync = archive_daily.fsync_directory

    def record_sync(path: Path) -> None:
        synced.append(Path(path))
        original_sync(path)

    monkeypatch.setattr(archive_daily, "fsync_directory", record_sync)
    result = _archiver(root, state_root).archive(daily.stem)
    assert root / "knowledge/daily" in synced
    assert root / "knowledge/daily/archive" in synced
    assert result.bag_path.parent in synced

    build = root / "seal-failure"
    build.mkdir()
    file = build / "payload"
    file.write_bytes(b"x")
    original_seal = archive_daily.DailyArchiver._set_archive_read_only

    def fail_seal(path: Path) -> None:
        if path == file:
            raise OSError("denied")
        original_seal(path)

    monkeypatch.setattr(
        archive_daily.DailyArchiver,
        "_set_archive_read_only",
        staticmethod(fail_seal),
    )
    with pytest.raises(PermissionError, match="seal"):
        archive_daily.DailyArchiver._seal(build)


def test_windows_read_only_acl_is_verified_and_fail_closed(tmp_path, monkeypatch) -> None:
    import archive_daily

    path = tmp_path / "bag"
    path.mkdir()
    monkeypatch.setattr(archive_daily.os, "name", "nt")
    monkeypatch.setattr(archive_daily, "_windows_acl_identity", lambda: "DOMAIN\\owner")
    calls = []

    def acl(command):
        calls.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout=b"bag DOMAIN\\owner:(OI)(CI)(RX)\r\n",
            stderr=b"",
        )

    monkeypatch.setattr(archive_daily, "_run_acl_command", acl)
    archive_daily.DailyArchiver._set_archive_read_only(path)
    assert any("DOMAIN\\owner:(OI)(CI)(RX)" in part for part in calls[0])

    monkeypatch.setattr(
        archive_daily,
        "_run_acl_command",
        lambda command: SimpleNamespace(returncode=1, stdout=b"", stderr=b"denied"),
    )
    with pytest.raises(PermissionError, match="ACL"):
        archive_daily.DailyArchiver._set_archive_read_only(path)


def test_cli_exposes_hot_and_transaction_retention_flags() -> None:
    import archive_daily

    args = archive_daily.parse_args(["--hot-days", "120", "--transaction-retention-days", "45"])
    assert args.hot_days == 120
    assert args.transaction_retention_days == 45


def test_only_archiver_uses_daily_archive_directory_rename_exception() -> None:
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    offenders = set()
    for path in scripts.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "publish_build.replace(final_bag)" in text:
            offenders.add(path.name)
    assert offenders == {"archive_daily.py"}
