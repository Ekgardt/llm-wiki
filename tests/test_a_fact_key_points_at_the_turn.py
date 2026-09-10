"""A user's own short facts index the turn they came from; the turn is what is read.

LongMemEval's key expansion, +9.4% recall. Keys are extracted at compile in
batches, kept in a disposable store under cache/, matched lexically and by
vector, and resolve to the turn's chunk; a key never reaches the model.
See `docs/research/2026-09-09-fact-keys-beside-the-turn.md`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import fact_keys  # noqa: E402
import query_memory  # noqa: E402
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


def _about_drums(text: str) -> float:
    return float("drum" in text.casefold())


def _encode(texts, is_query):
    if is_query:
        return np.asarray([[1.0, 1.0] for _ in texts])
    return np.asarray([[_about_drums(text), 1.0] for text in texts])


def test_user_turns_are_the_units_keyed(vault: Path) -> None:
    snapshot = collect_corpus(vault, code_roots=(), daily_paths=[DAILY])

    turns = fact_keys.user_turns(snapshot.chunks)

    # Short turns fold into one chunk; its user segments are keyed as one turn.
    assert len(turns) == 1
    assert turns[0].text.startswith("I'm thinking of sell") and "Korg B1" in turns[0].text


def test_keys_are_extracted_once_per_turn_and_found_by_word_and_by_vector(vault: Path, tmp_path: Path) -> None:
    snapshot = collect_corpus(vault, code_roots=(), daily_paths=[DAILY])
    store = fact_keys.KeyStore(tmp_path / "keys.sqlite3")

    keyed = fact_keys.key_turns(store, snapshot.chunks, _ask, _encode)
    again = fact_keys.key_turns(store, snapshot.chunks, _ask, _encode)

    assert (keyed, again) == (1, 0)
    assert store.count() == (1, 2)
    by_word = fact_keys.search(store, "Pearl Export drum", 5)
    by_vector = fact_keys.search(store, "musical instruments I own", 5, _encode)
    drum = next(turn for turn in fact_keys.user_turns(snapshot.chunks) if "drum" in turn.text)
    assert by_word[0]["byte_start"] == drum.byte_start
    assert by_vector[0]["byte_start"] == drum.byte_start
    store.close()


def test_an_unreadable_reply_keys_nothing_but_marks_the_turn_done(vault: Path, tmp_path: Path) -> None:
    snapshot = collect_corpus(vault, code_roots=(), daily_paths=[DAILY])
    store = fact_keys.KeyStore(tmp_path / "keys.sqlite3")

    fact_keys.key_turns(store, snapshot.chunks, lambda prompt, system_prompt: "not json", None)

    assert store.count() == (1, 0)
    assert fact_keys.search(store, "drum", 5) == []


def test_the_keys_leg_resolves_to_the_turn_and_never_shows_the_key(vault: Path, tmp_path: Path, monkeypatch) -> None:
    snapshot = collect_corpus(vault, code_roots=(), daily_paths=[DAILY])
    state = tmp_path / "state"
    store = fact_keys.KeyStore(fact_keys.store_path(state))
    fact_keys.key_turns(store, snapshot.chunks, _ask, None)
    store.close()
    monkeypatch.setattr(query_memory, "STATE_ROOT", state, raising=False)
    import memory_state

    monkeypatch.setattr(memory_state, "STATE_ROOT", state)
    seen: list[str] = []

    def generate(prompt: str, system_prompt: str, max_tokens: int) -> str:
        seen.append(prompt)
        return json.dumps(
            {"schema_version": "grounded-answer/v1", "status": "insufficient_evidence", "claims": [], "citations": [], "reason": "x"}
        )

    query_memory.grounded_qa(
        "Do I own a drum set?",
        vault=vault,
        snapshot=snapshot,
        retrieve=lambda limit: (),
        search=lambda query, limit, **window: (),
        generator=generate,
        profile="BASE",
    )

    assert "5-piece Pearl Export" in seen[0]
    assert "I own a 5-piece Pearl Export drum set" not in seen[0]
