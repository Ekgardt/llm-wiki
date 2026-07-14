from __future__ import annotations

import json
import os
import sqlite3
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest
from markdown_transaction import MarkdownChange, MarkdownCoordinator  # noqa: E402
from reliable_memory import sha256_bytes  # noqa: E402


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
        ("failure", "failure"),
        ("decision", "decision_evidence"),
        ("queue", "queue_reference"),
        ("legacy_queue", "legacy_queue_reference"),
        ("transaction", "active_transaction"),
        ("writer", "active_writer"),
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
    elif blocker == "failure":
        (state_root / "run" / "archive-failures.json").write_text(
            json.dumps({"daily_ids": [daily.stem]}), encoding="utf-8"
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
    elif blocker == "writer":
        with coordinator.writer_gate():
            result = _archiver(root, state_root).eligible(daily, hot_days=90)
            assert reason in result.reasons
            return

    result = _archiver(root, state_root).eligible(daily, hot_days=90)
    assert not result.eligible
    assert reason in result.reasons


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
    bag = validate_bag(receipt.bag_path)
    assert bag.manifest["logical_daily_id"] == daily.stem
    assert bag.manifest["source_hash"] == digest
    assert bag.manifest["payload_hash"] == digest
    assert bag.manifest["queue_preflight"]["passed"] is True
    assert bag.manifest["retention_days"] == 90
    assert bag.manifest["operations"][0]["state"] == "succeeded"
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
    assert EvidenceResolver(root).resolve(ref).bytes == source[evidence["byte_start"] :]


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
    assert bag.exists()
    records = list((state_root / "run/archive-quarantine").glob("*.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_bytes())["reason"] == "duplicate_hash_mismatch"


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
