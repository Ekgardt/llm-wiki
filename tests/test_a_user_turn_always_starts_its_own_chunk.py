"""A short user turn is a chunk of its own; a short assistant reply still folds.

A 91-character user turn ended a 2 101-character chunk that began with an assistant reply,
and the fact it stated ranked 53rd. Research:
`docs/research/2026-09-16-a-user-turn-always-starts-its-own-chunk.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import corpus_snapshot  # noqa: E402

LONG_REPLY = "**assistant:** " + ("a helpful paragraph about note taking apps. " * 12) + "\n\n"
SHORT_USER = "**user:** my commute takes 45 minutes each way.\n\n"
SHORT_REPLY = "**assistant:** glad to hear it!\n\n"
ENTRY = (
    "# Daily Session Memory — 2023-05-22\n\n"
    "## [21:18:00] session_end | answer_40a90d51\n\n"
    "_captured: 2023-05-22 21:18:00_\n\n"
    "**user:** I am looking for audiobook recommendations for my commute, something long.\n\n"
    + LONG_REPLY
    + SHORT_USER
    + SHORT_REPLY
)


def _chunks(tmp_path: Path):
    root = tmp_path / "vault"
    (root / "knowledge/daily").mkdir(parents=True)
    (root / "knowledge/notes").mkdir(parents=True)
    (root / "knowledge/projects").mkdir(parents=True)
    (root / "knowledge/daily/2023-05-22.md").write_text(ENTRY, encoding="utf-8")
    snapshot = corpus_snapshot.collect_corpus(root, daily_paths=("knowledge/daily/2023-05-22.md",))
    return [chunk.text for chunk in snapshot.chunks]


def test_the_short_user_turn_is_found_on_its_own(tmp_path) -> None:
    texts = _chunks(tmp_path)

    holding = [text for text in texts if "45 minutes each way" in text]

    assert len(holding) == 1
    assert holding[0].lstrip().startswith("**user:**")


def test_a_bare_acknowledgement_still_folds(tmp_path) -> None:
    root = tmp_path / "vault"
    (root / "knowledge/daily").mkdir(parents=True)
    (root / "knowledge/notes").mkdir(parents=True)
    (root / "knowledge/projects").mkdir(parents=True)
    (root / "knowledge/daily/2023-05-22.md").write_text(ENTRY + "**user:** thanks\n\n", encoding="utf-8")
    snapshot = corpus_snapshot.collect_corpus(root, daily_paths=("knowledge/daily/2023-05-22.md",))

    holding = [chunk.text for chunk in snapshot.chunks if "thanks" in chunk.text]

    assert len(holding) == 1
    assert holding[0].lstrip().startswith("**user:** my commute")


def test_the_short_assistant_reply_stays_with_the_turn_before_it(tmp_path) -> None:
    texts = _chunks(tmp_path)

    holding = [text for text in texts if "glad to hear it" in text]

    assert len(holding) == 1
    assert "45 minutes each way" in holding[0]
