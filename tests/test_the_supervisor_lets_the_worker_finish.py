"""The supervisor waits out the worker's own settling, and survives a file vanishing.

See docs/research/2026-09-25-the-supervisor-lets-the-worker-finish.md.
"""

from __future__ import annotations

from pathlib import Path

import mcp_server
import mcp_supervisor


def test_the_graceful_stop_outlasts_the_workers_inference_wait() -> None:
    assert mcp_supervisor.GRACEFUL_STOP_SECONDS > mcp_server.SHUTDOWN_INFERENCE_SECONDS


def test_a_file_removed_after_listing_changes_the_fingerprint_instead_of_raising(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    before = mcp_supervisor.code_fingerprint(tmp_path)
    listed = [tmp_path / "a.py", tmp_path / "gone.py"]
    monkeypatch.setattr(mcp_supervisor, "_source_files", lambda scripts: listed)

    after = mcp_supervisor.code_fingerprint(tmp_path)

    assert after != before


def test_the_worker_is_waited_for_before_it_is_signalled() -> None:
    waits: list[float] = []

    class Input:
        def close(self):
            pass

    class Worker:
        stdin = Input()

        def wait(self, timeout):
            waits.append(timeout)

        def terminate(self):
            raise AssertionError("a worker that exits on its own is never signalled")

        kill = terminate

    mcp_supervisor.stop_process(Worker())

    assert waits == [mcp_supervisor.GRACEFUL_STOP_SECONDS]
