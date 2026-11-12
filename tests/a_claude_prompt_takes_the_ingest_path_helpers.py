"""Helpers shared by test_a_claude_prompt_takes_the_ingest_path.py and the test files importing them."""
from __future__ import annotations


def _effective_host_timeout(event: str, hook: dict) -> float:
    # https://code.claude.com/docs/en/hooks, checked 2026-10-05.
    # Command hooks with async:true are not canceled by the host timeout.
    if hook.get("async"):
        return float("inf")
    default = {"UserPromptSubmit": 30}.get(event, 600)
    return hook.get("timeout", default)
