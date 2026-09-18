"""A refused compile leaves the holder's status, a lock is never seen empty, and a v3 retry waits.

Research: `docs/research/2026-09-14-the-small-integrity-gaps.md`.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def test_a_refused_compile_records_its_refusal_not_the_holders_status(monkeypatch):
    import compile_memory

    marks: list[str] = []
    arguments = type(
        "A",
        (),
        {"trigger": "auto", "discard_unusable_receipts": False, "lock_token": None},
    )
    monkeypatch.setattr(compile_memory, "parse_args", arguments)
    monkeypatch.setattr(
        compile_memory,
        "_acquire_compile_lock",
        lambda _token=None: (None, "lock held by another compile"),
    )
    monkeypatch.setattr(compile_memory, "_mark_started", lambda trigger: marks.append("started"))
    monkeypatch.setattr(compile_memory, "_mark_finished", lambda *a, **k: marks.append("finished"))
    monkeypatch.setattr(compile_memory, "_mark_refused", lambda trigger, reason: marks.append("refused"))

    assert (compile_memory.main(), marks) == (1, ["refused"])


def test_the_compile_lock_is_created_with_its_content(tmp_path, monkeypatch):
    import maybe_compile

    lock = tmp_path / "run" / "compile.pid"
    monkeypatch.setattr(maybe_compile, "LOCK_FILE", lock)
    seen: list[bytes] = []
    real_link = maybe_compile.os.link

    def observe(source, destination):
        real_link(source, destination)
        seen.append(Path(destination).read_bytes())

    monkeypatch.setattr(maybe_compile.os, "link", observe)

    claimed = maybe_compile._try_claim_lock()

    assert (claimed, seen[0].startswith(b"0\n"), maybe_compile._try_claim_lock()) == (True, True, False)


def test_a_v3_task_going_back_to_ready_waits_before_it_is_claimed_again():
    import memory_queue

    now = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
    delays = {memory_queue._retry_available_at("ready", 3, now) > now for _ in range(20)}
    immediate = [memory_queue._retry_available_at(state, 3, now) for state in ("blocked", "dead")]

    assert (True in delays, immediate) == (True, [now, now])
