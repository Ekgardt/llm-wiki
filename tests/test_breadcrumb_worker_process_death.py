"""A killed worker leaves enough evidence for a fresh worker, without clock edits."""
from __future__ import annotations

import sqlite3
import subprocess
import sys
import threading
from contextlib import closing
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from types import SimpleNamespace

# A separate Python process does not load pytest's conftest import setup.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import breadcrumb_evidence  # noqa: E402
import breadcrumb_protocol  # noqa: E402
import flush_memory  # noqa: E402
import llm_client  # noqa: E402
import memory_queue  # noqa: E402

from tests.slow_machine import LONG_TIMEOUT  # noqa: E402
from tests.test_breadcrumb_storage import SCOPE, _publish  # noqa: E402
from tests.test_breadcrumb_worker import _assert_complete, _no_model  # noqa: E402
from tests.test_queue_v3_capture_links import _coordinator, _queue  # noqa: E402

BOUNDARIES = ("receipt", "source", "journal", "terminal-file")


def _interrupt(target, name, after):
    import os

    original = getattr(target, name)

    def killed(*args, **kwargs):
        if after:
            original(*args, **kwargs)
        os._exit(73)

    setattr(target, name, killed)


def _crash_worker(root, boundary):
    queue, coordinator = _queue(root), _coordinator(root)
    _publish(queue, coordinator, {"prompt": "complete original-day evidence"})
    llm_client.call_llm_result = _no_model
    points = {
        "receipt": (flush_memory, "_index_capture_decision", True),
        "source": (breadcrumb_evidence, "publish_source", True),
        "journal": (flush_memory, "_commit_capture_markdown", True),
        "terminal-file": (queue, "complete_capture_terminal", False),
    }
    _interrupt(*points[boundary])
    flush_memory.run_capture_worker_once(
        queue, coordinator, handler_versions=(2,),
        process_missing=partial(flush_memory.process_new_capture, queue, coordinator),
    )
    raise AssertionError("the selected crash boundary was not reached")


def _killed_root(root, boundary):
    result = subprocess.run(
        [sys.executable, "-m", "tests.test_breadcrumb_worker_process_death", str(root), boundary],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
        timeout=LONG_TIMEOUT, check=False,
    )
    assert result.returncode == 73, result.stderr
    return root


def _persisted_expiry(root):
    with closing(sqlite3.connect(root / "run/queue-v3.candidate.sqlite3")) as database:
        task = database.execute("SELECT MAX(lease_expires_at) FROM tasks").fetchone()[0]
    with closing(sqlite3.connect(root / "run/markdown-transactions-v3.candidate.sqlite3")) as database:
        owner = database.execute("SELECT MAX(expires_at) FROM maintenance_owners").fetchone()[0]
    return max(datetime.fromisoformat(value.replace("Z", "+00:00")) for value in (task, owner))


def _recover(root):
    queue = memory_queue.MemoryQueue._from_v3_candidate(
        root / "run/queue-v3.candidate.sqlite3", state_root=root,
    )
    coordinator = _coordinator(root)
    work = partial(
        flush_memory.run_capture_worker_once, queue, coordinator, handler_versions=(2,),
        process_missing=partial(flush_memory.process_new_capture, queue, coordinator),
    )
    assert work()
    assert work() is None
    publication = SimpleNamespace(intent_id=breadcrumb_protocol.occurrence_identity(SCOPE))
    _assert_complete((queue, coordinator, publication, work))


def test_real_worker_death_recovers_every_committed_boundary(tmp_path, monkeypatch):
    roots = [_killed_root(tmp_path / boundary, boundary) for boundary in BOUNDARIES]
    expiry = max(_persisted_expiry(root) for root in roots)
    # The recorded real deadlines govern recovery; no lease, clock, owner or
    # process identity is edited to make recovery pass. All four waits overlap.
    threading.Event().wait(max(0, (expiry - datetime.now(timezone.utc)).total_seconds()))
    monkeypatch.setattr(llm_client, "call_llm_result", _no_model)
    for root in roots:
        _recover(root)


if __name__ == "__main__":
    _crash_worker(Path(sys.argv[1]), sys.argv[2])
