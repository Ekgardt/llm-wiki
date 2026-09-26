"""A day compiled in several parts archives with every part's receipt (audit 2026-09-26 B-1).

docs/research/2026-09-26-a-split-day-is-archived-with-every-part.md
"""
from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import pytest
from markdown_transaction import MarkdownCoordinator

_PATHS = {
    "MEMORY": "knowledge",
    "DAILY_DIR": "knowledge/daily",
    "KNOWLEDGE": "knowledge/notes",
    "INDEX": "knowledge/index.md",
    "LOG": "knowledge/log.md",
    "AGENTS": "AGENTS.md",
}


def _split_day() -> bytes:
    entries = [
        f"<!-- llm-wiki-operation:{minute:064x} -->\n## [10:{minute:02d}:00] event\n" + "evidence line\n" * 400
        for minute in range(8)
    ]
    return ("# day\n" + "\n".join(entries)).encode()


def _vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    import compile_memory

    root, state_root = tmp_path / "vault", tmp_path / "state"
    for relative in ("knowledge/daily/receipts", "knowledge/notes"):
        (root / relative).mkdir(parents=True)
    state_root.mkdir()
    for name in ("knowledge/index.md", "knowledge/log.md", "AGENTS.md"):
        (root / name).write_bytes(b"# placeholder\n")
    daily = root / "knowledge/daily/2026-01-01.md"
    daily.write_bytes(_split_day())
    monkeypatch.setattr(compile_memory, "ROOT", root)
    monkeypatch.setattr(compile_memory, "STATE_ROOT", state_root)
    for attribute, relative in _PATHS.items():
        monkeypatch.setattr(compile_memory, attribute, root / relative)
    return root, state_root, daily


def _compile_every_part(root: Path, state_root: Path, daily: Path) -> int:
    import compile_memory

    count = len(compile_memory.pack_compile_batches(compile_memory.snapshot_compile_inputs([daily]), model=None))
    for index in range(count):
        # Each batch commits the index, so the next one reads the vault again.
        batch = compile_memory.pack_compile_batches(compile_memory.snapshot_compile_inputs([daily]), model=None)[index]
        compile_memory.apply_compile_plan(
            batch.inputs,
            {"schema_version": "compile-plan/v2", "operations": []},
            action_key=f"{index:064x}",
            trigger="manual",
            coordinator=MarkdownCoordinator(root, state_root),
            completed_at="2026-07-01T00:00:00Z",
            batch=batch,
            provider_budget={"provider": "fake", "model": "fake-v1", "max_output_tokens": 4000},
        )
    with closing(sqlite3.connect(state_root / "run/markdown-transactions.sqlite3")) as connection, connection:
        connection.execute(
            'UPDATE "transaction" SET updated_at="2026-05-01T00:00:00Z" WHERE operation_id LIKE "compile:%"'
        )
    return count


def _archive(root: Path, state_root: Path, daily_id: str):
    from archive_daily import DailyArchiver

    clock = lambda: datetime(2026, 7, 14, 12, tzinfo=timezone.utc)  # noqa: E731
    return DailyArchiver(root, state_root, clock=clock).archive(daily_id)


def test_a_split_day_archives_one_receipt_per_part(tmp_path, monkeypatch) -> None:
    from evidence_resolver import validate_bag

    root, state_root, daily = _vault(tmp_path, monkeypatch)
    parts = _compile_every_part(root, state_root, daily)

    archived = _archive(root, state_root, daily.stem)
    bag = validate_bag(archived.bag_path, coordinator=MarkdownCoordinator(root, state_root), vault=root)
    embedded = sorted(path.name for path in archived.bag_path.glob("compile-receipt-*.md"))

    assert (parts > 1, archived.state, bag.manifest["schema_version"]) == (True, "archived", "archive-manifest/v2")
    assert embedded == [f"compile-receipt-{index}.md" for index in range(parts)]


def test_a_part_quote_resolves_from_the_archive(tmp_path, monkeypatch) -> None:
    from evidence_resolver import EvidenceRef, EvidenceResolver, _daily_part_bounds, daily_entries
    from reliable_memory import sha256_bytes

    root, state_root, daily = _vault(tmp_path, monkeypatch)
    content = daily.read_bytes()
    _compile_every_part(root, state_root, daily)
    start, end = _daily_part_bounds(content)[1]
    part = content[start:end]
    block_id, block_start, block_end = daily_entries(part)[0]
    ref = EvidenceRef(daily.stem, sha256_bytes(part), block_id, block_start, block_end)

    _archive(root, state_root, daily.stem)
    shutil.rmtree(state_root / "run")

    assert EvidenceResolver(root, state_root=state_root).resolve(ref).bytes == part[block_start:block_end]


def test_the_schema_admits_as_many_parts_as_a_day_can_hold() -> None:
    import json

    from evidence_resolver import ARCHIVE_SCHEMAS, MAX_COMPILE_PARTS

    schema = json.loads(ARCHIVE_SCHEMAS["archive-manifest/v2"].read_text(encoding="utf-8"))

    assert schema["properties"]["compile_parts"]["maxItems"] == MAX_COMPILE_PARTS
