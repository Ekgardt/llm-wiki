"""A doctor check the budget cut short says it did not finish; it does not say `error`.

Under the default 5 s budget `queue` and `claims` reported `error` and advised
`--repair` on a healthy vault, because the deadline is caught together with real
read errors. See docs/research/2026-09-25-a-late-check-is-unfinished-not-broken.md.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import doctor


def _claims_database(state_root: Path) -> None:
    (state_root / "cache").mkdir(parents=True)
    with sqlite3.connect(state_root / "cache" / "claims.sqlite3") as database:
        database.execute("CREATE TABLE placeholder(value)")


def test_a_read_the_deadline_stopped_is_unfinished_and_still_blocks_deletion(tmp_path: Path) -> None:
    _claims_database(tmp_path)
    late = time.monotonic() - 1

    check = doctor._unfinished_when_late(doctor._claim_check(tmp_path, tmp_path, late), late)

    assert (check["status"], check["details"]["budget_exhausted"]) == ("degraded", True)
    assert check["details"]["deletion_codes"] == ["claims_state_unreadable"]
    assert "--repair" not in check["message"]


def test_a_read_error_inside_the_budget_stays_an_error(tmp_path: Path) -> None:
    _claims_database(tmp_path)
    late = time.monotonic() - 1
    unreadable = doctor._claim_check(tmp_path, tmp_path, late)

    check = doctor._unfinished_when_late(unreadable, time.monotonic() + 60)

    assert check["status"] == "error"


def test_every_check_doctor_collects_passes_the_late_rule(tmp_path: Path, monkeypatch) -> None:
    seen: list[str] = []

    def observed(check: dict, _deadline: float) -> dict:
        seen.append(check["id"])
        return check

    monkeypatch.setattr(doctor, "_unfinished_when_late", observed)

    checks = doctor._collect_checks(tmp_path, tmp_path, tmp_path, doctor._as_utc(None), time.monotonic() - 1)

    assert seen == [check["id"] for check in checks]
