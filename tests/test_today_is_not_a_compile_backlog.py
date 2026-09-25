"""Today's daily log is not a compile backlog; a past day that changed is.

Every capture appends to today's log and the nightly compiles it, so counting it
kept the health resource partial all day. See
docs/research/2026-09-25-today-is-not-a-compile-backlog.md.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import mcp_server
import memory_state


def _vault(tmp_path: Path, monkeypatch, *days: str) -> None:
    daily = tmp_path / "knowledge" / "daily"
    daily.mkdir(parents=True)
    for day in days:
        (daily / f"{day}.md").write_text(f"# {day}\n", encoding="utf-8")
    monkeypatch.setattr(memory_state, "ROOT", tmp_path)
    monkeypatch.setattr(memory_state, "load_state", lambda: {"compiled_daily_hashes": {}})


def test_todays_log_is_not_counted(tmp_path: Path, monkeypatch) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    _vault(tmp_path, monkeypatch, now.date().isoformat(), now.astimezone().date().isoformat())

    assert mcp_server._vault_status()["compile_backlog"] == 0


def test_an_uncompiled_past_day_is_counted(tmp_path: Path, monkeypatch) -> None:
    _vault(tmp_path, monkeypatch, "2026-01-02")

    assert mcp_server._vault_status()["compile_backlog"] == 1
