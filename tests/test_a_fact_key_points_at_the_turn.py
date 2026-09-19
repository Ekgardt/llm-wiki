"""A user's own short facts index the turn they came from; the turn is what is read.

LongMemEval's key expansion, +9.4% recall. Keys are extracted at compile in
batches and kept in a disposable store under cache/; the generation's search
table is their one reader (`tests/test_the_keys_are_indexed_beside_the_turn.py`).
See `docs/research/2026-09-09-fact-keys-beside-the-turn.md`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import fact_keys  # noqa: E402
from corpus_snapshot import collect_corpus  # noqa: E402

DAILY = "knowledge/daily/2023-05-22.md"
BODY = (
    "# 2023-05-22\n\n## [10:00:00] session_end | s\n\n_captured: 2023-05-22 10:00:00_\n\n"
    "**user:** I'm thinking of selling my old drum set, a 5-piece Pearl Export, which I haven't played in years.\n\n"
    "**assistant:** You could list it on a marketplace.\n\n"
    "**user:** How do I keep my Korg B1 piano in shape?\n\n"
    "**assistant:** Keep it dust free.\n"
)


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "knowledge" / "daily").mkdir(parents=True)
    (root / "knowledge" / "notes").mkdir(parents=True)
    (root / DAILY).write_text(BODY, encoding="utf-8")
    return root


def _ask(prompt: str, system_prompt: str) -> str:
    assert system_prompt == fact_keys.EXTRACT_SYSTEM_PROMPT
    turns = prompt.count("<turn id=")
    keys = {"0": ["I own a 5-piece Pearl Export drum set", "I have not played the drum set in years"]}
    if turns > 1:
        keys["1"] = ["I own a Korg B1 piano"]
    return json.dumps(keys)


def test_user_turns_are_the_units_keyed(vault: Path) -> None:
    snapshot = collect_corpus(vault, code_roots=(), daily_paths=[DAILY])

    turns = fact_keys.user_turns(snapshot.chunks)

    # Since 2026-09-16 a user turn always begins its own chunk, so each is keyed on its own.
    # See `docs/research/2026-09-16-a-user-turn-always-starts-its-own-chunk.md`.
    assert len(turns) == 2
    assert turns[0].text.startswith("I'm thinking of sell")
    assert "Korg B1" in turns[1].text


def test_an_unreadable_reply_keys_nothing_and_leaves_the_turn_to_ask_again(vault: Path, tmp_path: Path) -> None:
    """Since 2026-09-14 a turn the reply did not cover stays pending.

    See `docs/research/2026-09-14-an-error-is-not-an-answer.md`.
    """
    snapshot = collect_corpus(vault, code_roots=(), daily_paths=[DAILY])
    store = fact_keys.KeyStore(tmp_path / "keys.sqlite3")

    fact_keys.key_turns(store, snapshot.chunks, lambda prompt, system_prompt: "not json")
    retried = fact_keys.key_turns(store, snapshot.chunks, _ask)

    assert (retried, store.count()[0]) == (2, 2)
