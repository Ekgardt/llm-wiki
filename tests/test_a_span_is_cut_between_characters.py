"""A byte ceiling cut Russian prose in half and took the whole index with it.

`MAX_SPAN_BYTES` is a byte count. A span carrying no paragraph break and no
newline was cut at exactly that byte — and on any text that is not ASCII, that
lands inside a character about three times in four. `_chunks` then decoded the
span with `errors="strict"`, raised `UnicodeDecodeError`, and the exception
travelled up through the corpus collector and aborted the nightly generation
build.

Measured on this vault 2026-09-05: no generation had been published since
2026-08-30, `catalog_state.active_generation_id` was NULL, and every answer for
five days came from the lexical leg alone. The only trace was a per-row
`fallback_reason: "generation_unavailable"` and one line in `doctor`.

See `docs/research/2026-09-05-an-index-that-went-missing-without-saying-so.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import corpus_snapshot  # noqa: E402


def _unbroken_cyrillic(byte_length: int) -> bytes:
    """Prose with no newline anywhere, so only the byte ceiling can cut it."""
    word = "тест "
    text = word * (byte_length // len(word.encode("utf-8")) + 2)
    return text.encode("utf-8")


def test_every_piece_of_a_long_unbroken_paragraph_decodes() -> None:
    content = _unbroken_cyrillic(corpus_snapshot.MAX_SPAN_BYTES * 3)

    pieces = corpus_snapshot._split_span(content, (0, len(content), ()))

    for start, end, _ancestry in pieces:
        content[start:end].decode("utf-8", errors="strict")


def test_the_pieces_still_cover_the_whole_span_without_overlap() -> None:
    """Backing off a boundary must not drop or duplicate a byte."""
    content = _unbroken_cyrillic(corpus_snapshot.MAX_SPAN_BYTES * 3)

    pieces = corpus_snapshot._split_span(content, (0, len(content), ()))

    assert pieces[0][0] == 0
    assert pieces[-1][1] == len(content)
    for earlier, later in zip(pieces, pieces[1:]):
        assert earlier[1] == later[0]


def test_a_cut_inside_a_character_walks_back_to_its_start() -> None:
    content = "аб".encode("utf-8")  # two bytes each

    assert corpus_snapshot._character_boundary(content, 1) == 0
    assert corpus_snapshot._character_boundary(content, 2) == 2
    assert corpus_snapshot._character_boundary(content, 3) == 2


def test_the_end_of_the_content_is_a_boundary() -> None:
    content = b"abc"

    assert corpus_snapshot._character_boundary(content, 3) == 3


def test_ascii_is_never_moved() -> None:
    content = b"hello world"

    assert corpus_snapshot._character_boundary(content, 5) == 5


def test_a_four_byte_character_is_not_split_either() -> None:
    content = "🙂".encode("utf-8")

    for index in (1, 2, 3):
        assert corpus_snapshot._character_boundary(content, index) == 0


def test_a_capture_that_loses_the_race_takes_another_pass(monkeypatch, tmp_path) -> None:
    """Every night from 2026-08-30 the build died on the first lost race.

    A pass costs 3.6 seconds and never fails on a quiet vault; a session
    appending to today's daily log on every tool call made it fail under
    maintenance, and the whole nightly generation went with it.
    """
    vault = tmp_path / "vault"
    (vault / "knowledge" / "notes").mkdir(parents=True)
    (vault / "knowledge" / "notes" / "alpha.md").write_text("Alpha.", encoding="utf-8")
    attempts = {"n": 0}
    original = corpus_snapshot._capture

    def flaky(root, policy, deadline, cancelled):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise corpus_snapshot.CorpusChanged("corpus source changed during collection")
        return original(root, policy, deadline, cancelled)

    monkeypatch.setattr(corpus_snapshot, "_capture", flaky)

    snapshot = corpus_snapshot.collect_corpus(vault)

    assert attempts["n"] == 3
    assert snapshot.sources


def test_a_vault_that_never_holds_still_is_still_refused(monkeypatch, tmp_path) -> None:
    """Retrying is not ignoring: the fence still has to pass once."""
    import pytest

    vault = tmp_path / "vault"
    (vault / "knowledge" / "notes").mkdir(parents=True)
    (vault / "knowledge" / "notes" / "alpha.md").write_text("Alpha.", encoding="utf-8")

    def always_moving(root, policy, deadline, cancelled):
        raise corpus_snapshot.CorpusChanged("corpus source changed during collection")

    monkeypatch.setattr(corpus_snapshot, "_capture", always_moving)

    with pytest.raises(corpus_snapshot.CorpusChanged, match="never held still"):
        corpus_snapshot.collect_corpus(vault)
