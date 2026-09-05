"""A complete answer must not be lost to a sentence of commentary.

Providers wrap the requested JSON in a Markdown fence and then, often enough to
matter, say something either side of it. The parser used to accept a reply only
when it was *exactly* one fence, so those replies were discarded as invalid
JSON — measured over 200 questions on 2026-09-02, fifteen of them, every one
carrying a complete document.

Taking the fence is not trusting it. The document still validates against the
closed schema and every claim still faces its citation gates; the prose around
the fence is discarded and never shown.

See `knowledge/notes/a-failing-claim-does-not-destroy-the-answer-decision.md`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import query_memory  # noqa: E402

DOCUMENT = {
    "schema_version": "grounded-answer/v1",
    "status": "answered",
    "claims": [{"text": "Alpha is enabled.", "citation_ids": ["E1"]}],
    "citations": [],
    "reason": None,
}
BODY = json.dumps(DOCUMENT, ensure_ascii=False, indent=2)


@pytest.mark.parametrize(
    "reply",
    (
        BODY,
        f"```json\n{BODY}\n```",
        f"```json\n{BODY}\n```\n\nSo the answer is 600 followers.",
        f"I have enough evidence to answer.\n\n```json\n{BODY}\n```",
        f"Sorry, wrong tool.\n\n```json\n{BODY}\n```\n\nThat is the answer.",
    ),
    ids=("bare", "fenced", "prose-after", "prose-before", "prose-both"),
)
def test_a_document_inside_a_fence_is_read_whatever_surrounds_it(reply: str) -> None:
    assert query_memory._parsed_answer(reply) == DOCUMENT


def test_a_reply_with_no_json_is_still_refused() -> None:
    with pytest.raises(query_memory.GroundedQAError):
        query_memory._parsed_answer("The designer you mean is Jessica Poole.")


def test_a_truncated_document_is_still_refused() -> None:
    """Recovering a fence is not repairing one."""
    with pytest.raises(query_memory.GroundedQAError):
        query_memory._parsed_answer('```json\n{"status": "answ\n```')


def test_an_empty_reply_is_refused() -> None:
    with pytest.raises(query_memory.GroundedQAError):
        query_memory._parsed_answer("")
