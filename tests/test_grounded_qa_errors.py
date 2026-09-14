"""A refusal carries what the provider said, bounded and redacted.

Eighteen of nineteen rows of a LongMemEval run said only "returned invalid
JSON" while the provider had said "API Error: ... safeguards flagged this
message". Research:
`docs/research/2026-09-13-an-unparsable-answer-must-say-what-it-said.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import query_memory  # noqa: E402


def _refusal(raw: str) -> str:
    with pytest.raises(query_memory.GroundedQAError) as refusal:
        query_memory._parsed_answer(raw)
    return str(refusal.value)


def test_the_refusal_names_what_the_provider_said():
    message = _refusal("API Error: safeguards flagged this message. Try another model.")

    assert "API Error: safeguards flagged this message" in message


def test_the_refusal_states_how_long_the_answer_was():
    message = _refusal("not json at all")

    assert "15 chars" in message


def test_a_long_answer_is_cut_to_the_bound():
    message = _refusal("x" * 5000)

    assert len(message) < 400


def test_newlines_do_not_break_the_one_line_message():
    message = _refusal("API Error:\n\n  flagged\nline three")

    assert "API Error: flagged line three" in message


def test_a_secret_in_the_answer_is_not_repeated():
    message = _refusal("API Error: token sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAA rejected")

    assert "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAA" not in message


def test_an_empty_answer_is_still_its_own_refusal():
    message = _refusal("")

    assert "no response" in message


def test_a_valid_document_is_returned_unchanged():
    assert query_memory._parsed_answer('{"status": "ok"}') == {"status": "ok"}
