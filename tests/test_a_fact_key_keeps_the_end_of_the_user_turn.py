"""A fact at the end of an admitted source span must reach the extractor."""
from __future__ import annotations

import json

import fact_keys
import pytest
from corpus_snapshot import MAX_SPAN_BYTES, collect_corpus


@pytest.mark.parametrize(
    "prefix", ["a" * 1700, "я" * 1700, "a" * 1550 + "😀" * 150],
    ids=("ascii", "cyrillic", "escaped-boundary"),
)
def test_the_complete_admitted_turn_reaches_the_extractor(prefix):
    words = prefix + " My bicycle is named Polaris."
    text = "**user:** " + words + "\n\n**assistant:** Understood."
    assert len(text.encode()) < MAX_SPAN_BYTES
    assert fact_keys._user_text(text) == words


def _snapshot(tmp_path):
    path = tmp_path / "knowledge/daily/2026-10-01.md"
    path.parent.mkdir(parents=True)
    words = "Earlier context. " * 110 + "My bicycle is named Polaris."
    body = "# 2026-10-01\n\n## [10:00:00] session_end | proof\n\n**user:** " + words + "\n"
    path.write_text(body)
    return collect_corpus(tmp_path, code_roots=(), daily_paths=(str(path.relative_to(tmp_path)),)), words


def test_the_tail_fact_keeps_the_original_source_span(tmp_path):
    snapshot, words = _snapshot(tmp_path)
    turns = fact_keys.user_turns(snapshot.chunks)
    assert len(turns) == 1
    assert turns[0].text == words
    chunk = next(item for item in snapshot.chunks if "**user:**" in item.text)
    assert turns[0].span_sha256 == chunk.span_sha256
    assert (turns[0].byte_start, turns[0].byte_end) == (chunk.byte_start, chunk.byte_end)
    assert turns[0].source_sha256 == chunk.source_sha256


def test_keying_sends_the_complete_turn_and_keys_its_original_span(tmp_path):
    snapshot, words = _snapshot(tmp_path)
    seen = []

    def extract(prompt, system):
        seen.append((prompt, system))
        assert words in prompt
        return json.dumps({"0": ["My bicycle is named Polaris."]})

    path = tmp_path / "keys.sqlite3"
    store = fact_keys.KeyStore(path)
    try:
        assert fact_keys.key_turns(store, snapshot.chunks, extract) == 1
    finally:
        store.close()
    assert len(seen) == 1
    chunk = next(item for item in snapshot.chunks if "**user:**" in item.text)
    assert fact_keys.keys_by_span(path)[chunk.span_sha256] == "My bicycle is named Polaris."
