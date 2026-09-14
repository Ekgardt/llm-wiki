"""A redriven flush task marks its block with the same operation as the task it copies.

The operation id was the task id, and a redrive copy has a new one, so a block that
was written before the original died was written again. Research:
`docs/research/2026-09-14-a-redriven-flush-appends-nothing-new.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

PAYLOAD = {"prompt": "summarize", "system_prompt": "system", "day": "2026-07-14", "event": "session-end", "session_id": "s1"}


def test_the_original_and_its_redrive_append_under_one_operation(tmp_path, monkeypatch):
    import daily_log_append
    import flush_memory
    import llm_client
    import memory_queue

    operations: list[str] = []
    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
    monkeypatch.setattr(llm_client, "call_llm", lambda *args, **kwargs: "major body")
    monkeypatch.setattr(flush_memory, "_classify_response", lambda result: ("major", result))
    monkeypatch.setattr(daily_log_append, "locked_append_once", lambda path, block, operation: operations.append(operation) or True)

    for task_id in ("original", "redrive-copy"):
        memory_queue._manual_processor({"id": task_id, "type": "flush", "payload": dict(PAYLOAD)})

    assert (len(operations), len(set(operations))) == (2, 1)
