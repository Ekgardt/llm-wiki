"""A foreground subagent's report is kept in the session record.

The record dropped every tool result, so the conclusion a foreground subagent
returned to the session was missing from it. A background launch returns only a
receipt; its report arrives later as a notification the record already keeps. See
docs/research/2026-09-24-a-subagents-report-is-kept-with-its-session.md.
"""

from __future__ import annotations

import json

import session_evidence


def _line(entry: dict) -> str:
    return json.dumps(entry)


def _call(call_id: str, name: str = "Agent") -> str:
    block = {"type": "tool_use", "id": call_id, "name": name, "input": {"prompt": "look"}}
    return _line({"type": "assistant", "message": {"content": [block]}})


def _result(call_id: str, text: str, tool_use_result: object = None) -> str:
    block = {"type": "tool_result", "tool_use_id": call_id, "content": [{"type": "text", "text": text}]}
    entry = {"type": "user", "message": {"content": [block]}}
    if tool_use_result is not None:
        entry["toolUseResult"] = tool_use_result
    return _line(entry)


def test_a_foreground_report_is_kept_and_other_tool_output_is_not() -> None:
    transcript = "\n".join(
        [
            _call("agent-1"),
            _result("agent-1", "The cache is keyed wrongly.", {"status": "completed"}),
            _call("read-1", name="Read"),
            _result("read-1", "file bytes"),
        ]
    )

    rendered = session_evidence.render_transcript(transcript)

    assert "**subagent report:** The cache is keyed wrongly." in rendered
    assert "file bytes" not in rendered


def test_an_older_host_names_the_tool_task() -> None:
    transcript = "\n".join([_call("task-1", name="Task"), _result("task-1", "Found it.")])

    assert "**subagent report:** Found it." in session_evidence.render_transcript(transcript)


def test_a_background_launch_receipt_is_not_a_report() -> None:
    transcript = "\n".join(
        [_call("agent-2"), _result("agent-2", "Async agent launched.", {"isAsync": True})]
    )

    assert "subagent report" not in session_evidence.render_transcript(transcript)


def test_a_long_report_is_cut_and_says_so() -> None:
    long_report = "x" * (session_evidence.MAX_SUBAGENT_REPORT_CHARS + 10)
    transcript = "\n".join([_call("agent-3"), _result("agent-3", long_report)])

    rendered = session_evidence.render_transcript(transcript)

    assert rendered.endswith(session_evidence.SUBAGENT_REPORT_CUT)
    assert "x" * (session_evidence.MAX_SUBAGENT_REPORT_CHARS + 1) not in rendered
