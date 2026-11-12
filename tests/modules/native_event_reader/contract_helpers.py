"""Helpers shared by test_contract.py and the test files importing them."""

import json


def _frame(prompt, **changes):
    event = dict(schema_version="1.0", event_type="user_prompt", payload={"prompt": prompt},
                 agent="codex", session="s", project=None, worktree=None,
                 severity=None, parent_event_id=None, source_event_id=None)
    event.update(changes)
    return json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
