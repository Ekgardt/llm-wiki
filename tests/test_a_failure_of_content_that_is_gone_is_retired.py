"""A compile failure recorded for bytes the day no longer has is retired.

See docs/research/2026-09-25-a-failure-of-content-that-is-gone-is-retired.md.
"""

from __future__ import annotations

from pathlib import Path

import compile_memory
from memory_queue import MemoryQueue
from reliable_memory import sha256_bytes

DAY = "knowledge/daily/2026-09-01.md"


def test_only_the_failure_of_current_content_survives(tmp_path: Path, monkeypatch) -> None:
    vault, state = tmp_path / "vault", tmp_path / "state"
    (vault / "knowledge/daily").mkdir(parents=True)
    state.mkdir()
    before = b"# 2026-09-01\n\n## [10:00:00] first\nfirst words\n"
    after = before + b"\n## [11:00:00] second\nsecond words\n"
    (vault / DAY).write_bytes(after)
    monkeypatch.setattr(compile_memory, "ROOT", vault)
    queue = MemoryQueue(state)
    for content in (before, after):
        queue.record_source_failure(DAY, sha256_bytes(content), error_code="ValueError", producer="compile")
    queue.record_source_failure("knowledge/daily/2026-08-01.md", "a" * 64, error_code="ValueError", producer="compile")

    compile_memory._retire_stale_source_failures(state)

    assert queue.source_failure_keys() == [(DAY, sha256_bytes(after))]


def test_the_adopted_queue_lists_its_failure_keys(tmp_path: Path) -> None:
    """The live vault reads through the adopted (v3) queue, not the legacy one."""
    import memory_queue

    database = tmp_path / "run" / "queue-v3.candidate.sqlite3"
    memory_queue.initialize_queue_v3_candidate(database, source_v2=None)
    reader = memory_queue._QueueV3CandidateReader(database)
    reader.record_source_failure(DAY, "b" * 64, error_code="ValueError", producer="compile")

    assert reader.source_failure_keys() == [(DAY, "b" * 64)]
