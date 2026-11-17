"""Helpers shared by test_archive_daily_bagit.py and the test files importing them."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from markdown_transaction import MarkdownCoordinator


@pytest.fixture
def archive_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    root = tmp_path / "vault"
    state_root = tmp_path / "state"
    state_root.mkdir()
    for relative in ("knowledge/daily/receipts", "knowledge/notes"):
        (root / relative).mkdir(parents=True)
    (root / "knowledge/index.md").write_bytes(b"# Knowledge Index\n")
    (root / "knowledge/log.local.md").write_bytes(b"# Session Memory Log\n")
    (root / "AGENTS.md").write_bytes(b"contract\n")
    daily = root / "knowledge/daily/2026-01-01.md"
    daily.write_bytes(b"# day\n## [evt-1] event\ncompiled evidence\n")

    from llm_wiki import compile_pipeline_apply, compile_pipeline_batches, compile_pipeline_sources

    monkeypatch.setenv("LLM_WIKI_ROOT", str(root))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))
    inputs = compile_pipeline_sources.snapshot_compile_inputs([daily])
    batch = compile_pipeline_batches.pack_compile_batches(inputs, model=None)[0]
    compile_pipeline_apply.apply_compile_plan(
        inputs,
        {"schema_version": "compile-plan/v2", "operations": []},
        action_key="b" * 64,
        trigger="manual",
        coordinator=MarkdownCoordinator(root, state_root),
        completed_at="2026-07-01T00:00:00Z",
        batch=batch,
        provider_budget={
            "provider": "fake",
            "model": "fake-v1",
            "max_output_tokens": 4000,
        },
    )
    with closing(
        sqlite3.connect(state_root / "run/markdown-transactions.sqlite3")
    ) as connection, connection:
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
        clock=lambda: datetime(2026, 7, 14, 12, tzinfo=UTC),
        **kwargs,
    )
