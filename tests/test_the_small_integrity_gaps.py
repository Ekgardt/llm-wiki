"""A refused compile leaves the holder's status, a lock is never seen empty, and a v3 retry waits.

Research: `docs/research/2026-09-14-the-small-integrity-gaps.md`.
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import queue_core

from tests.compile_paths import point_compile_path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def test_a_refused_compile_records_its_refusal_not_the_holders_status(monkeypatch):
    import compile_memory

    marks: list[str] = []
    arguments = type(
        "A",
        (),
        {
            "trigger": "auto",
            "discard_unusable_receipts": False,
            "lock_token": None,
            "all": False,
            "dry_run": False,
        },
    )
    point_compile_path(monkeypatch, "parse_args", arguments)
    point_compile_path(monkeypatch, "acquire_compile_lock", lambda _token=None: (None, "lock held by another compile"))
    point_compile_path(monkeypatch, "_mark_started", lambda trigger: marks.append("started"))
    point_compile_path(monkeypatch, "mark_finished", lambda *a, **k: marks.append("finished"))
    point_compile_path(monkeypatch, "_mark_refused", lambda trigger, reason: marks.append("refused"))
    monkeypatch.setattr(compile_memory, "refresh_generation_after_compile", lambda: None)

    assert (compile_memory.main(), marks) == (1, ["refused"])


def test_the_compile_lock_is_created_with_its_content(tmp_path, monkeypatch):
    import compile_lock

    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))
    seen: list[bytes] = []
    real_link = compile_lock.os.link

    def observe(source, destination):
        real_link(source, destination)
        seen.append(Path(destination).read_bytes())

    monkeypatch.setattr(compile_lock.os, "link", observe)

    claimed = compile_lock.try_claim_lock()

    assert (claimed, seen[0].startswith(b"0\n"), compile_lock.try_claim_lock()) == (True, True, False)


def test_a_v3_task_going_back_to_ready_waits_before_it_is_claimed_again():

    now = datetime(2026, 9, 14, 12, tzinfo=UTC)
    delays = {queue_core.retry_available_at("ready", 3, now) > now for _ in range(20)}
    immediate = [queue_core.retry_available_at(state, 3, now) for state in ("blocked", "dead")]

    assert (True in delays, immediate) == (True, [now, now])
