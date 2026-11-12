"""Helpers shared by test_compile_keeps_complete_tool_lines.py and the test files importing them."""

from breadcrumb_decision import journal_block
from llm_wiki import compile_pipeline_sources
from reliable_memory import canonical_json_bytes


def _tool_parts():
    record = {"event_type": "post_tool_use", "payload": {
        "tool_name": "exec_command", "target": "я" * 12000}}
    raw = canonical_json_bytes(record)
    anchor = {"occurred_at": "2026-10-02T12:34:56+00:00", "intent_id": "physical-tool-control"}
    head = "knowledge/raw/sessions/2026-10-02/physical-tool-control.breadcrumb.md"
    day = b"# 2026-10-02\n" + journal_block(anchor, head, raw).encode()
    parts = compile_pipeline_sources.daily_parts("knowledge/daily/2026-10-02.md", day)
    start = day.index(raw)
    covering = [part for part in parts if part.byte_start < start + len(raw)
                and part.byte_end > start]
    return covering, raw
