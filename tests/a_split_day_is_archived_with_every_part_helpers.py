"""Helpers shared by test_a_split_day_is_archived_with_every_part.py and the test files importing them."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from markdown_transaction import MarkdownCoordinator


def _split_day() -> bytes:
    entries = [
        f"<!-- llm-wiki-operation:{minute:064x} -->\n## [10:{minute:02d}:00] event\n" + "evidence line\n" * 400
        for minute in range(8)
    ]
    return ("# day\n" + "\n".join(entries)).encode()


def _vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    root, state_root = tmp_path / "vault", tmp_path / "state"
    for relative in ("knowledge/daily/receipts", "knowledge/notes"):
        (root / relative).mkdir(parents=True)
    state_root.mkdir()
    for name in ("knowledge/index.md", "knowledge/log.md", "AGENTS.md"):
        (root / name).write_bytes(b"# placeholder\n")
    daily = root / "knowledge/daily/2026-01-01.md"
    daily.write_bytes(_split_day())
    monkeypatch.setenv("LLM_WIKI_ROOT", str(root))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))
    return root, state_root, daily


def _compile_every_part(root: Path, state_root: Path, daily: Path) -> int:
    from llm_wiki import compile_pipeline_apply, compile_pipeline_batches, compile_pipeline_sources

    count = len(compile_pipeline_batches.pack_compile_batches(compile_pipeline_sources.snapshot_compile_inputs([daily]), model=None))
    for index in range(count):
        # Each batch commits the index, so the next one reads the vault again.
        batch = compile_pipeline_batches.pack_compile_batches(compile_pipeline_sources.snapshot_compile_inputs([daily]), model=None)[index]
        compile_pipeline_apply.apply_compile_plan(
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

    clock = lambda: datetime(2026, 7, 14, 12, tzinfo=UTC)  # noqa: E731
    return DailyArchiver(root, state_root, clock=clock).archive(daily_id)
