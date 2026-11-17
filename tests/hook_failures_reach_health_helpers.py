"""Helpers shared by test_hook_failures_reach_health.py and the test files importing them."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

NOW = datetime(2026, 8, 29, 21, 10, tzinfo=UTC)


def _trail(state_root: Path, lines: list[str]) -> Path:
    logs = state_root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / "hook-errors.log"
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return path


def _line(at: datetime, kind: str = "project checkpoint") -> str:
    return f"[{at.isoformat()}] {kind}: ValueError: something went wrong"
