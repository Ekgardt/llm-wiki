"""The vault stands must not be able to score by retrieving their own sheet.

Both sheets live under `benchmark/`, which is an approved corpus root, so they
are indexed with the pages they grade. Each stand used to name one path in a
constant, and the constant went stale: neither knew about
`tests/test_intent_conditional_trust.py`, and the application stand dropped the
*other* stand's sheet from its ranking while leaving its own in.

These tests pin the property rather than the list: a file that states a case in
the stand's own words is dropped, whoever wrote it and whenever it appeared.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _extra in (ROOT / "scripts", ROOT / "benchmark"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

import answer_key  # noqa: E402
import run_vault_application as application  # noqa: E402
import run_vault_retrieval as retrieval  # noqa: E402
from retrieval_paths import Observation  # noqa: E402

RETRIEVAL_SHEET = "benchmark/vault-retrieval-v1.json"
APPLICATION_SHEET = "benchmark/vault-application-v1.json"


def _corpora() -> tuple[dict, dict]:
    return (
        json.loads((ROOT / RETRIEVAL_SHEET).read_text(encoding="utf-8")),
        json.loads((ROOT / APPLICATION_SHEET).read_text(encoding="utf-8")),
    )


def test_both_sheets_are_derived_from_what_the_files_say():
    found = answer_key.answer_key_paths(ROOT, _corpora())

    assert RETRIEVAL_SHEET in found
    assert APPLICATION_SHEET in found


def test_a_file_nobody_named_still_joins_the_answer_key():
    """The defect the constant had: this one was never in either stand's list."""
    found = answer_key.answer_key_paths(ROOT, _corpora())

    assert "tests/test_intent_conditional_trust.py" in found


def test_no_gold_page_can_ever_be_dropped():
    corpora = _corpora()
    golds = answer_key.gold_paths(corpora[0]) | answer_key.gold_paths(corpora[1])

    assert answer_key.answer_key_paths(ROOT, corpora).isdisjoint(golds)


def _leaky_vault(tmp_path: Path) -> Path:
    corpus = {
        "corpus_id": "x",
        "schema_version": "vault-retrieval/v1",
        "thresholds": {"min_hit_at_5": 0.6, "min_gain_over_grep_at_5": 0.1},
        "cases": [
            {
                "case_id": "only-case",
                "question": "a question written nowhere else at all",
                "gold_path": "knowledge/notes/gold.md",
            }
        ],
    }
    (tmp_path / "benchmark").mkdir()
    (tmp_path / "benchmark/vault-retrieval-v1.json").write_text(
        json.dumps(corpus), encoding="utf-8"
    )
    (tmp_path / "docs").mkdir()
    return tmp_path


def test_a_new_copy_of_a_question_is_found_by_content(tmp_path):
    vault = _leaky_vault(tmp_path)
    (vault / "docs/notes.md").write_text(
        "someone quoted a question written nowhere else at all here", encoding="utf-8"
    )

    found = answer_key.sheets(vault)

    assert found == {"benchmark/vault-retrieval-v1.json", "docs/notes.md"}


def test_a_file_that_states_no_case_is_left_alone(tmp_path):
    vault = _leaky_vault(tmp_path)
    (vault / "docs/notes.md").write_text("unrelated prose", encoding="utf-8")

    assert answer_key.sheets(vault) == {"benchmark/vault-retrieval-v1.json"}


def _fake_observe(monkeypatch, paths: list[str]) -> None:
    def _observe(path: str, query: str, limit: int) -> Observation:
        return Observation(path=path, result_paths=list(paths)[:limit])

    monkeypatch.setattr(retrieval, "observe", _observe)


def test_the_ranking_drops_the_sheets_and_still_returns_five(monkeypatch):
    pages = [f"knowledge/notes/page-{index}.md" for index in range(6)]
    _fake_observe(monkeypatch, [RETRIEVAL_SHEET, APPLICATION_SHEET, *pages])

    ranked = retrieval.product_ranking("anything", 5, "api")

    assert ranked == pages[:5]


def test_the_scored_text_never_contains_a_sheet():
    text = application._text_of(ROOT, [RETRIEVAL_SHEET, APPLICATION_SHEET])

    assert text.strip() == ""


def test_the_grep_baseline_drops_the_same_sheets():
    dropped = frozenset({"docs/anything.md"})
    ranked = retrieval.grep_ranking(ROOT, "compile receipt evidence", 5, dropped)

    assert "docs/anything.md" not in ranked
