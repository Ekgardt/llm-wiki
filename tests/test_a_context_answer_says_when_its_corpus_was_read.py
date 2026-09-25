"""A context answer says when its corpus was read.

`get_context` reads Markdown at request time and named a content hash where the
envelope looks for a generation id, so its `index_timestamp` was always empty. See
docs/research/2026-09-25-a-context-answer-says-when-its-corpus-was-read.md.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import mcp_server  # noqa: E402
import memory_state  # noqa: E402


def test_a_context_answer_carries_the_moment_its_corpus_was_collected(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "knowledge/notes").mkdir(parents=True)
    monkeypatch.setattr(memory_state, "ROOT", tmp_path)
    before = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)

    data = mcp_server._get_context(["page"])
    stamp = mcp_server._index_timestamp("get_context", data)

    after = dt.datetime.now(dt.timezone.utc)
    assert before <= dt.datetime.fromisoformat(stamp) <= after


def test_a_context_hash_is_never_read_as_a_generation(monkeypatch) -> None:
    monkeypatch.setattr(mcp_server, "_generation_built_ns", lambda generation: 1)

    assert mcp_server._index_timestamp("get_context", {"corpus_generation": "a" * 64}) is None
