"""The fact keys are read by the index column alone, and a turn is asked a bounded number of nights.

Third audit, 2026-09-17 (retrieval M3 and L6 with generations M4). LongMemEval measured the
key merged into the turn's own index entry as the good shape and the keys kept as separate
retrieval items as the worse one, so the separate leg is gone and with it the question of
whether product keys need vectors. A turn whose reply never covers it is asked `MAX_ATTEMPTS`
nights and then left to the index, so one failing batch cannot spend the budget every night.
See `docs/research/2026-09-17-the-keys-live-in-the-index-and-nowhere-else.md`.
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
    "**user:** I sold my old drum set, a 5-piece Pearl Export.\n\n"
    "**assistant:** Noted.\n\n"
    "**user:** I keep a Korg piano, a Korg synth and a Korg tuner in the studio.\n\n"
    "**assistant:** Noted.\n"
)


@pytest.fixture
def chunks(tmp_path: Path) -> tuple:
    root = tmp_path / "vault"
    (root / "knowledge" / "daily").mkdir(parents=True)
    (root / "knowledge" / "notes").mkdir(parents=True)
    (root / DAILY).write_bytes(BODY.encode("utf-8"))
    return tuple(collect_corpus(root, code_roots=(), daily_paths=[DAILY]).chunks)


def _covers_neither(prompt: str, system_prompt: str) -> str:
    return json.dumps({})


def _covers_both(prompt: str, system_prompt: str) -> str:
    return json.dumps({"0": ["I sold a Pearl Export drum set"], "1": ["I keep a Korg piano"]})


def _nights(store, chunks: tuple, ask, count: int) -> list[int]:
    """How many turns still wait at the start of each of `count` nights."""
    waiting = []
    for _night in range(count):
        waiting.append(len(fact_keys.waiting_turns(store, chunks)))
        fact_keys.key_turns(store, chunks, ask)
    return waiting


def test_a_turn_no_reply_ever_covers_stops_being_asked(chunks: tuple, tmp_path: Path) -> None:
    store = fact_keys.KeyStore(tmp_path / "keys.sqlite3")

    waiting = _nights(store, chunks, _covers_neither, fact_keys.MAX_ATTEMPTS + 1)
    after = (len(fact_keys.waiting_turns(store, chunks)), store.given_up())
    store.close()

    assert waiting == [2] * fact_keys.MAX_ATTEMPTS + [0]
    assert after == (0, 2)


def test_a_provider_that_says_nothing_spends_no_attempt(chunks: tuple, tmp_path: Path) -> None:
    """An outage is not the turn's fault: three silent nights must not retire every turn."""
    store = fact_keys.KeyStore(tmp_path / "keys.sqlite3")

    _nights(store, chunks, lambda prompt, system_prompt: None, fact_keys.MAX_ATTEMPTS + 1)
    still_waiting = len(fact_keys.waiting_turns(store, chunks))
    keyed = fact_keys.key_turns(store, chunks, _covers_both)
    store.close()

    assert (still_waiting, keyed) == (2, 2)


def test_a_turn_already_asked_waits_behind_a_turn_never_asked(chunks: tuple, tmp_path: Path) -> None:
    store = fact_keys.KeyStore(tmp_path / "keys.sqlite3")
    drums, korg = fact_keys.user_turns(chunks)
    store.note_asked([drums])

    order = [turn.span_sha256 for turn in fact_keys.waiting_turns(store, chunks)]
    store.close()

    assert order == [korg.span_sha256, drums.span_sha256]


def test_the_key_store_has_no_second_reader_left_in_the_answer_path() -> None:
    """The leg that read the store directly is gone; the index column is the reader."""
    import query_memory

    assert not hasattr(query_memory, "_with_keys_leg")
    assert not hasattr(fact_keys, "search")
