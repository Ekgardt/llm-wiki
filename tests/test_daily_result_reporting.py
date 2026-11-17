"""The command's human-readable result agrees with the recorded write result."""
from __future__ import annotations

from argparse import Namespace

import codex_memory
import pytest


@pytest.mark.parametrize("written", [False, True])
@pytest.mark.parametrize("flush_spawned", [False, True])
def test_daily_output_names_only_completed_work(monkeypatch, tmp_path, capsys, written, flush_spawned):
    outcome = {
        "slug": "app", "heartbeat_recorded": False, "daily_log_written": written,
        "flush_spawned": flush_spawned, "transcript_path": "session.jsonl", "returncode": 0,
    }
    monkeypatch.setattr(codex_memory, "ingest_event", lambda *args, **kwargs: outcome)
    result = codex_memory.command_daily_log(
        Namespace(cwd=str(tmp_path), reason="end", session_id="session", force_stub=True, json=False)
    )
    output = capsys.readouterr().out
    assert result == 0
    assert ("Daily log tagged" in output) is written
    assert ("Daily log not written" in output) is (not written)
    assert ("Flush spawned" in output) is flush_spawned
