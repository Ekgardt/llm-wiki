"""The applied-memory stand must stay fair and must not read its own answers."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "benchmark") not in sys.path:
    sys.path.insert(0, str(ROOT / "benchmark"))


def test_every_expected_token_appears_verbatim_in_its_gold_page():
    """A token the gold page does not contain makes the case unwinnable."""
    from run_vault_application import load_corpus

    corpus = load_corpus()
    missing = [
        (case["case_id"], token)
        for case in corpus["cases"]
        for token in case["expected_tokens"]
        if token not in (ROOT / case["gold_path"]).read_text(encoding="utf-8")
    ]

    assert missing == []


def test_the_task_sheet_never_counts_as_its_own_answer():
    """The sheet carries every token; reading it would pass every case.

    The path used to be a module constant, `_SELF`. It is now derived from what
    the files say — see `benchmark/answer_key.py` — so the test asks the derived
    set for it rather than naming it a second time.
    """
    from run_vault_application import _text_of, dropped_paths

    sheet = "benchmark/vault-application-v1.json"

    assert sheet in dropped_paths(ROOT)
    assert _text_of(ROOT, [sheet]).strip() == ""


def test_a_missing_token_fails_the_case():
    from run_vault_application import applied

    assert applied("uses --ff-only merge", ["--ff-only"])
    assert not applied("uses a fast-forward merge", ["--ff-only"])
