"""A key reply is read under the names it used.

Third audit, 2026-09-17. See
`docs/research/2026-09-17-a-key-reply-is-read-as-written-and-a-leg-votes-once.md`; the leg
that voted was removed by
`docs/research/2026-09-17-the-keys-live-in-the-index-and-nowhere-else.md`.
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
    (root / DAILY).write_text(BODY, encoding="utf-8")
    return tuple(collect_corpus(root, code_roots=(), daily_paths=[DAILY]).chunks)


def test_a_reply_that_pads_its_turn_numbers_still_keys_the_turns(chunks: tuple, tmp_path: Path) -> None:
    store = fact_keys.KeyStore(tmp_path / "keys.sqlite3")
    reply = json.dumps({"00": ["I sold a Pearl Export drum set"], "01": ["I keep a Korg piano"]})

    keyed = fact_keys.key_turns(store, chunks, lambda prompt, system_prompt: reply)

    assert (keyed, store.count()) == (2, (2, 2))
    store.close()
