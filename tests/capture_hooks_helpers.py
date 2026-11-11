"""Helpers shared by test_capture_hooks.py and the test files importing them."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

from tests.breadcrumb_storage_helpers import _bundle
from tests.breadcrumb_worker_helpers import ingress as ingress


def _run_capture_with_stdin(module_name: str, stdin_payload: dict | str) -> int:
    """Helper: invoke capture script's main() with simulated stdin."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    mod = __import__(module_name)

    # Simulate stdin
    if isinstance(stdin_payload, dict):
        stdin_text = json.dumps(stdin_payload)
    else:
        stdin_text = stdin_payload

    with patch.object(sys, "stdin", io.StringIO(stdin_text)):
        return mod.main()


HOOKS = (("user_prompt_capture", {"prompt": "да"}),
         ("post_tool_capture", {"tool_name": "Bash", "tool_input": {"command": "pwd"}}))


def _hook_payload(payload, event_id="host-once"):
    return {**payload, "event_id": event_id, "session_id": "direct-session",
            "timestamp": "2026-09-29T12:00:00+00:00", "cwd": "/fixture"}


def _published_content(queue):
    ready = list((queue.state_root / "run/capture-intents/ready").rglob("*.json"))
    assert len(ready) == 1
    identity = json.loads(ready[0].read_bytes())["intent_id"]
    return _bundle(queue.state_root, identity).content


def _deliver_hook(ingress):
    from functools import partial

    import flush_memory

    queue, coordinator = ingress
    return flush_memory.run_capture_worker_once(
        queue, coordinator, handler_versions=(2,),
        process_missing=partial(flush_memory.process_new_capture, queue, coordinator),
    )
