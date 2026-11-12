#!/usr/bin/env python3
"""Retire old LSP failure roots under `run/lsp/`, keeping the newest as evidence.

A failed language-server start leaves `run/lsp/<owner-nonce>/failure.json`, and the
contract left such roots "for the operator", who had no command for them: 80 roots
sat on the live vault on 2026-09-23. A root whose owner is not live and whose failure
is older than `MAX_AGE_DAYS` is removed once more than `KEEP_NEWEST` such roots exist.
Live owners and younger evidence are never touched. Run by the nightly pass.
Research: `docs/research/2026-09-23-the-rest-of-the-live-audit.md`.
"""
from __future__ import annotations

import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from llm_wiki import runtime_roots

sys.path.insert(0, str(Path(__file__).resolve().parent))

KEEP_NEWEST = 20
# The 14 days of the LSP evidence contract (CLAUDE.md: the nightly retires failure roots older than 14 days).
MAX_AGE_DAYS = 14.0
# One retirement scan's budget, shared by retire_own_call_transcripts. basis unknown — value predates measurement; review when a scan ends unfinished.
SCAN_BUDGET_SECONDS = 20.0


def retirable(record: dict) -> bool:
    """Proven dead, holding failure evidence older than the retention bound."""
    age = record.get("failure_age_days")
    if record.get("processes_dead") is not True:
        return False
    if record.get("live") or not record.get("failure_evidence"):
        return False
    return isinstance(age, (int, float)) and age > MAX_AGE_DAYS


def dead_evidence(owners: list[dict]) -> list[dict]:
    """Proven-dead owners with failure evidence, oldest last."""
    dead = [r for r in owners if r.get("failure_evidence") and r.get("processes_dead") is True]
    return sorted(dead, key=_age_of)


def _age_of(record: dict) -> float:
    age = record.get("failure_age_days")
    if isinstance(age, (int, float)):
        return float(age)
    return 0.0


def _owner_records(state_root: Path, now: datetime) -> list[dict]:
    from llm_wiki.doctor_lsp import (
        scan_lsp_owners,
        snapshot_lsp_runtime,
    )

    deadline = time.monotonic() + SCAN_BUDGET_SECONDS
    snapshots, _unreadable, absent = snapshot_lsp_runtime(state_root, deadline)
    if absent:
        return []
    owners, _codes, _scan_unreadable = scan_lsp_owners(snapshots, now, deadline)
    return owners


def retirable_roots(state_root: Path, now: datetime) -> list[Path]:
    """The failure roots to remove: the oldest beyond the newest `KEEP_NEWEST`."""
    beyond_kept = dead_evidence(_owner_records(state_root, now))[KEEP_NEWEST:]
    lsp_root = state_root / "run" / "lsp"
    return [lsp_root / str(r["owner_nonce"]) for r in beyond_kept if retirable(r)]


def retire(state_root: Path, now: datetime | None = None) -> int:
    """Remove the retirable roots; the count removed."""
    from lsp_process import remove_dead_owner_root

    removed = 0
    for root in retirable_roots(state_root, now or datetime.now(UTC)):
        remove_dead_owner_root(root)
        removed += int(not root.exists())
    return removed


def main() -> int:

    removed = retire(runtime_roots.state_root())
    print(f"retire_lsp_evidence: removed {removed} failure root(s) older than {MAX_AGE_DAYS:g} days")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
