"""A JSON document written after the model's notes is still the reply.

29 of 34 LongMemEval replies refused as invalid JSON were a prose reading followed
by one complete, schema-valid answer document, bare. Research:
`docs/research/2026-09-14-the-document-after-the-notes.md`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import aggregation_pass  # noqa: E402
import query_memory  # noqa: E402

DOCUMENT = {"schema_version": "grounded-answer/v1", "status": "answered", "claims": []}
NOTES = 'Working: - E1 (2023-05-23): user says "a 10% discount {first purchase}".\n\n'


def test_a_document_after_notes_is_the_reply():
    assert query_memory.reply_document(NOTES + json.dumps(DOCUMENT)) == DOCUMENT


def test_the_last_object_is_the_document_when_notes_quote_one():
    reply = 'E2 shows {"size": 3} in a config.\n' + json.dumps(DOCUMENT)

    assert query_memory.reply_document(reply) == DOCUMENT


def test_a_fenced_document_still_wins():
    fenced = "Here it is:\n```json\n" + json.dumps(DOCUMENT) + "\n```\nDone."

    assert query_memory.reply_document(fenced) == DOCUMENT


def test_prose_without_a_document_is_still_refused_with_its_excerpt():
    with pytest.raises(query_memory.GroundedQAError, match="invalid JSON .*claims:"):
        query_memory._parsed_answer("working: E1 says 3:1.\n\nclaims:\n- ratio is 3:1 (E1)")


def test_the_passes_beside_the_answer_read_the_same_way():
    reply = "Grouping the mentions.\n" + json.dumps({"groups": [[0, 1]]})

    assert aggregation_pass._parsed_groups(reply, 2) == [[0, 1]]
