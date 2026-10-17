"""An omitted answer remains pending; a run stays finite and serves fresh turns first."""

from __future__ import annotations

import json
from pathlib import Path

import fact_keys
import pytest
from corpus_snapshot import collect_corpus

from tests.test_the_keys_have_one_reader_and_a_turn_is_not_asked_forever import (
    BODY,
    DAILY,
    _covers_both,
    _covers_neither,
)


@pytest.fixture
def chunks(tmp_path: Path) -> tuple:
    root = tmp_path / "vault"
    path = root / DAILY
    path.parent.mkdir(parents=True)
    path.write_bytes(BODY.encode())
    return tuple(collect_corpus(root, code_roots=(), daily_paths=[DAILY]).chunks)


@pytest.fixture
def store(tmp_path: Path):
    opened = fact_keys.KeyStore(tmp_path / "keys.sqlite3")
    yield opened
    opened.close()


@pytest.mark.parametrize("previous_attempts", [3, 4, 100])
def test_an_unanswered_turn_stays_pending(chunks: tuple, store, previous_attempts: int) -> None:
    turns = fact_keys.user_turns(chunks)
    for _ in range(previous_attempts):
        store.note_asked(turns)
    assert fact_keys.waiting_turns(store, chunks) == turns


def test_a_later_complete_reply_recovers_the_keys(chunks: tuple, store) -> None:
    for _ in range(3):
        fact_keys.key_turns(store, chunks, _covers_neither)
    assert fact_keys.key_turns(store, chunks, _covers_both) == 2
    assert fact_keys.waiting_turns(store, chunks) == []
    assert store.count() == (2, 2)


def test_an_incomplete_reply_does_not_repeat_within_the_same_run(chunks: tuple, store) -> None:
    calls = []

    def ask(prompt: str, system_prompt: str) -> str:
        calls.append(prompt)
        return "{}"

    turns = fact_keys.user_turns(chunks)
    for _ in range(3):
        store.note_asked(turns)
    assert fact_keys.key_turns(store, chunks, ask) == 0
    assert len(calls) == 1
    assert set(store.asked().values()) == {4}


def test_a_fresh_turn_uses_the_remaining_budget_first(chunks: tuple, store, monkeypatch) -> None:
    clock = [0.0]
    prompts = []
    monkeypatch.setattr(fact_keys, "BATCH_TURNS", 1)
    monkeypatch.setattr(fact_keys.time, "monotonic", lambda: clock[0])

    def ask(prompt: str, system_prompt: str) -> str:
        prompts.append(prompt)
        clock[0] = 2.0
        return json.dumps({"0": ["a fact from this turn"]})

    old, fresh = fact_keys.user_turns(chunks)
    for _ in range(3):
        store.note_asked([old])
    assert fact_keys.key_turns(store, chunks, ask, deadline=1.0) == 1
    assert prompts == [fact_keys._batch_prompt([fresh])]
    assert fact_keys.waiting_turns(store, chunks) == [old]


def test_status_counts_uncovered_attempts_without_retiring_them(chunks: tuple, store) -> None:
    store.note_asked(fact_keys.user_turns(chunks))
    assert store.uncovered() == 2
    fact_keys.key_turns(store, chunks, _covers_both)
    assert store.uncovered() == 0
