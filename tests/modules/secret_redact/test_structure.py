"""Structured redaction preserves values, JSON and line framing."""
import json

import secret_redact

TURN = {
    "type": "assistant",
    "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {
        "command": "export PASSWORD=hunter2pass", "description": "set the password"}}]},
}


def test_a_transcript_line_stays_json_and_loses_its_secret() -> None:
    line = json.dumps(TURN, separators=(",", ":")) + "\n"

    redacted = secret_redact.redact_jsonl(line)
    record = json.loads(redacted)

    command = record["message"]["content"][0]["input"]["command"]
    assert (command, "hunter2pass" in redacted, redacted.endswith("\n")) == (
        "export PASSWORD=[REDACTED]", False, True
    )


def test_a_value_under_a_secret_named_key_is_blanked_everywhere() -> None:
    payload = {"password": "short", "nested": [{"api_key": "x"}], "note": "keep"}

    assert secret_redact.redact_structure(payload) == {
        "password": "[REDACTED]", "nested": [{"api_key": "[REDACTED]"}], "note": "keep"
    }


def test_the_redaction_walk_passes_a_non_string_key_to_the_encoder() -> None:
    walked = secret_redact.redact_structure({1: "plain", "token": "secret-value"})

    assert walked == {1: "plain", "token": "[REDACTED]"}
