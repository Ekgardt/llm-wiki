"""A conversation is found by the round and read with its neighbours.

The 2026-09-02 decision cut sessions into 4 KB paragraph pieces; the model
read 4 KB to find one sentence and the reranker never fit its pairs into
its bound. Now a rendered conversation splits at every user turn — the
round the LongMemEval authors found retrieves best — a retrieved round is
delivered with the round either side of it, and only the top-ranked entry
comes in whole.
See `docs/research/2026-09-08-tokens-are-a-retrieval-unit-problem.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from context_budget import ContextBudget  # noqa: E402
from corpus_snapshot import MAX_SPAN_BYTES, collect_corpus  # noqa: E402
from query_memory import WHOLE_ENTRIES_ENV, build_grounded_context  # noqa: E402

DAILY = "knowledge/daily/2023-05-01.md"


def _round(index: int, words: int = 40) -> str:
    user = f"**user:** Round {index}: I visited the {'Austin ' * words}festival."
    reply = f"**assistant:** Reply {index}: {'that sounds lovely ' * words}."
    return user + "\n\n" + reply


def _session(name: str, rounds: int) -> str:
    return (
        f"## [10:00:00] session_end | {name}\n\n_captured: 2023-05-01 10:00:00_\n\n"
        + "\n\n".join(_round(index) for index in range(rounds))
        + "\n"
    )


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "knowledge" / "daily").mkdir(parents=True)
    (root / "knowledge" / "notes").mkdir(parents=True)
    (root / DAILY).write_text(
        "# 2023-05-01\n\n" + _session("sess_a", 6) + "\n" + _session("sess_b", 3), encoding="utf-8"
    )
    return root


def _pieces(snapshot, session: str) -> list:
    return sorted(
        (chunk for chunk in snapshot.chunks if session in " ".join(chunk.heading_ancestry)),
        key=lambda chunk: chunk.byte_start,
    )


def test_a_conversation_splits_at_every_user_turn(vault: Path) -> None:
    snapshot = collect_corpus(vault, code_roots=(), daily_paths=[DAILY])

    pieces = _pieces(snapshot, "sess_a")

    assert len(pieces) == 6
    assert all(piece.text.lstrip().startswith(("## ", "**user:**")) for piece in pieces)
    assert all(piece.byte_end - piece.byte_start <= MAX_SPAN_BYTES for piece in pieces)
    assert pieces[0].text.startswith("## [10:00:00] session_end | sess_a")


def test_a_short_round_stays_with_the_round_before_it(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    (root / "knowledge" / "daily").mkdir(parents=True)
    body = (
        "# 2023-05-01\n\n## [10:00:00] session_end | s\n\n"
        + _round(0)
        + "\n\n**user:** thanks\n\n**assistant:** you are welcome\n"
    )
    (root / DAILY).write_text(body, encoding="utf-8")

    snapshot = collect_corpus(root, code_roots=(), daily_paths=[DAILY])

    assert len([chunk for chunk in snapshot.chunks if len(chunk.heading_ancestry) == 2]) == 1


def _context(vault: Path, snapshot, candidates: tuple):
    return build_grounded_context(
        snapshot, candidates, vault=vault, profile="BASE", budget=ContextBudget(None, 122_880, 1200, 512)
    )


def test_a_retrieved_round_stands_alone_and_the_top_entry_comes_whole(
    vault: Path, monkeypatch
) -> None:
    monkeypatch.delenv(WHOLE_ENTRIES_ENV, raising=False)
    snapshot = collect_corpus(vault, code_roots=(), daily_paths=[DAILY])
    a_pieces, b_pieces = _pieces(snapshot, "sess_a"), _pieces(snapshot, "sess_b")

    context = _context(vault, snapshot, (b_pieces[0], a_pieces[3]))

    starts = [item.byte_start for item in context.evidence]
    assert starts[: len(b_pieces)] == [piece.byte_start for piece in b_pieces]
    assert starts[len(b_pieces) :] == [a_pieces[3].byte_start]


def test_the_switch_at_zero_leaves_only_the_round(vault: Path, monkeypatch) -> None:
    monkeypatch.setenv(WHOLE_ENTRIES_ENV, "0")
    snapshot = collect_corpus(vault, code_roots=(), daily_paths=[DAILY])
    a_pieces = _pieces(snapshot, "sess_a")

    context = _context(vault, snapshot, (a_pieces[0],))

    assert [item.byte_start for item in context.evidence] == [a_pieces[0].byte_start]
