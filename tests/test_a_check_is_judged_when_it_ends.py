"""A store found broken before the deadline stays an error, whatever a later check costs.

Audit 2026-09-26 B-19, docs/research/2026-09-26-a-check-is-judged-when-it-ends.md
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import doctor


def test_a_corrupt_queue_stays_an_error_when_a_later_check_runs_late(tmp_path, monkeypatch) -> None:
    root, state = tmp_path / "vault", tmp_path / "state"
    (state / "run").mkdir(parents=True)
    root.mkdir()
    (state / "run" / "queue.sqlite3").write_bytes(os.urandom(8192))
    budget = 2.0

    def late_claims(*_args, **_kwargs) -> dict:
        time.sleep(budget + 0.5)
        return {"id": "claims", "status": "ok", "message": "", "details": {}}

    monkeypatch.setattr(doctor, "_claim_check", late_claims)
    deadline = time.monotonic() + budget

    checks = doctor._collect_checks(root, state, tmp_path, datetime.now(timezone.utc), deadline)

    queue = next(check for check in checks if check["id"] == "queue")
    assert queue["status"] == "error"
