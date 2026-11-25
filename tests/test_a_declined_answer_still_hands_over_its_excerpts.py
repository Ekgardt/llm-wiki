"""Reading Б: the memory does not decide alone that it holds no answer.

When the reader declines, the answering agent still gets the excerpts the
reader was shown and the measured coverage, and a hint to search wider when
the best excerpt covers too little of the question. An answered question
carries its citations and nothing more.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import mcp_server  # noqa: E402
from corpus_snapshot import collect_corpus  # noqa: E402
from query_memory import grounded_qa  # noqa: E402

DECLINED = json.dumps(
    {
        "schema_version": "grounded-answer/v1",
        "status": "insufficient_evidence",
        "claims": [],
        "citations": [],
        "reason": "Nothing names the boss.",
    }
)


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "knowledge" / "notes").mkdir(parents=True)
    (root / "knowledge" / "daily").mkdir(parents=True)
    (root / "knowledge" / "notes" / "shop.md").write_text(
        "---\ntype: concept\nsource_authority: user\nconfidence: high\n---\n\n"
        "# Shop\n\nI bought the game at the store downtown.\n",
        encoding="utf-8",
    )
    return root


def _declined(vault: Path, question: str) -> dict:
    snapshot = collect_corpus(vault)
    chunk = next(item for item in snapshot.chunks if item.source_path.endswith("shop.md"))
    return grounded_qa(
        question,
        vault=vault,
        snapshot=snapshot,
        candidates=(chunk,),
        generator=lambda prompt, system_prompt, max_tokens: DECLINED,
        profile="BASE",
    )


def test_a_declined_answer_carries_the_excerpts_and_a_hint_when_they_are_weak(vault: Path) -> None:
    document = _declined(vault, "Which boss did I finally defeat in the dark souls game?")

    assert document["status"] == "insufficient_evidence"
    assert [entry["relative_path"] for entry in document["excerpts"]] == ["knowledge/notes/shop.md"]
    assert "store downtown" in document["excerpts"][0]["text"]
    assert "search wider" in document["hint"]
    assert "boss" in document["hint"]


def test_excerpts_that_cover_the_question_come_without_a_hint(vault: Path) -> None:
    document = _declined(vault, "At which store was the game bought?")

    assert document["excerpts"]
    assert "hint" not in document


def test_the_tool_warns_with_the_hint_beside_the_reason() -> None:
    quality = mcp_server._grounded_abstention_quality({"reason": "Nothing names the boss.", "hint": "Search wider."})

    assert quality["warnings"] == ["Nothing names the boss.", "Search wider."]
