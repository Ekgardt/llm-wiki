"""Question admission keeps the whole request and checks the actual prompt window."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import query_memory  # noqa: E402
from corpus_snapshot import collect_corpus  # noqa: E402

from tests.test_grounded_qa import _answer_for_prompt, _write_page  # noqa: E402


def test_a_long_question_reaches_the_generator_whole(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    _write_page(vault, "alpha.md", "Alpha is enabled.")
    snapshot = collect_corpus(vault)
    question = "Is Alpha enabled? " + "Additional operating context for this request. " * 400
    seen = []
    monkeypatch.setattr(query_memory, "_sentence_encoder", lambda: None)

    def generate(prompt, _system, _max_tokens):
        seen.append(prompt)
        return _answer_for_prompt(prompt)

    result = query_memory.grounded_qa(question, vault=vault, snapshot=snapshot,
        candidates=snapshot.chunks, generator=generate)
    assert result["status"] == "answered"
    assert len(seen) == 1
    assert "<question>\n" + question.strip() + "\n</question>" in seen[0]


def test_a_question_over_the_actual_window_refuses_before_a_model_call(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    _write_page(vault, "alpha.md", "Alpha is enabled.")
    snapshot = collect_corpus(vault)
    monkeypatch.setattr(query_memory, "_sentence_encoder", lambda: None)
    question = "Is Alpha enabled? " + "Additional operating context. " * 3000
    monkeypatch.setattr(query_memory, "_resolved_profile",
        lambda *args: pytest.fail("oversized prompt reached query analysis"))
    with pytest.raises(query_memory.GroundedQAError, match="shared context budget"):
        query_memory.grounded_qa(question, vault=vault, snapshot=snapshot,
            candidates=snapshot.chunks, generator=lambda *args: pytest.fail("model was called"))


@pytest.mark.parametrize("question", [None, True, "", " \n "])
def test_a_grounded_question_is_still_a_nonempty_string(question):
    with pytest.raises(query_memory.GroundedQAError, match="non-empty string"):
        query_memory._require_bounded_question(question)
