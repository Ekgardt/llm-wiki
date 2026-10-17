"""Qualify the existing durable publisher before reusing it for breadcrumbs.

This does not claim the prompt/tool adapter already calls that publisher.
"""

from __future__ import annotations

import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import integration_adapter
import pytest
from event_envelope import build_event_envelope
from reliable_memory import sha256_bytes

from tests.slow_machine import LONG_TIMEOUT
from tests.test_queue_v3_capture_links import _coordinator, _queue


@contextmanager
def _other_process_writer(state_root: Path):
    command = """
import sys
from pathlib import Path
from tests.test_queue_v3_capture_links import _coordinator
with _coordinator(Path(sys.argv[1])).writer_gate():
    print('writer-held', flush=True)
    sys.stdin.read()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", command, str(state_root)],
        cwd=Path(__file__).resolve().parents[1],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert process.stdout.readline().strip() == "writer-held"
        yield
    finally:
        _, error = process.communicate("", timeout=LONG_TIMEOUT)
    assert process.returncode == 0, error


def _record(text: str) -> tuple[dict, bytes]:
    envelope = build_event_envelope(
        event_type="pre_compact",
        payload={"reason": "qualification", "transcript_path": None},
        agent="claude", session="qualification", source_event_id="qualification-event",
    )
    source = integration_adapter._capture_source_record(envelope, "qualification", None, text)
    return integration_adapter._encoded_capture_record(source)


@pytest.mark.parametrize(
    "text", ["one event", '\u65e5\\"\n' * 1000, "x" * 900_000],
    ids=["small", "unicode-escaping", "near-current-intent-bound"],
)
def test_durable_publication_does_not_wait_for_the_markdown_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str,
) -> None:
    queue = _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    monkeypatch.setattr(integration_adapter, "STATE_ROOT", tmp_path)
    record, encoded = _record(text)
    intent_id = record["intent_id"]
    integration_adapter._ensure_capture_intent_directories(tmp_path, intent_id)
    pending = f"run/capture-intents/pending/{intent_id[:2]}/{intent_id}.json"
    ready = f"run/capture-intents/ready/{intent_id[:2]}/{intent_id}.json"
    with _other_process_writer(tmp_path):
        started = time.monotonic()
        integration_adapter._publish_capture_files_and_task(
            queue, coordinator, intent_id=intent_id, payload=encoded,
            intent_sha256=sha256_bytes(encoded), pending_relative=pending,
            ready_relative=ready,
        )
        elapsed = time.monotonic() - started
        assert (tmp_path / ready).read_bytes() == encoded
        assert queue.claim_capture("qualification-worker") is not None
    print(f"publication bytes={len(encoded)} seconds={elapsed:.6f}")
