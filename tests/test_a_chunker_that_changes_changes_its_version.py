"""The chunks of two fixed sources are pinned under the extractor version's name.

Change where a source is cut and this fails until `EXTRACTOR_VERSION` moves and a
new pin is written beside it: every reader that asks "was this generation made by
today's extractor" reads that string. Research:
`docs/research/2026-09-17-a-chunker-that-changes-changes-its-version.md`.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

REPLY = "**assistant:** " + "A long reply that says many things about the question asked. " * 5 + "\n\n"
CONVERSATION = (
    "# Session\n\n"
    + "**user:** "
    + "What should I plant in a shaded garden bed this spring, and how often do I water it? " * 3
    + "\n\n"
    + REPLY
    + "**user:** I moved to Lisbon in March and adopted a grey cat named Pixel.\n\n"
    + REPLY
    + "**user:** thanks\n\n"
    + REPLY
)
PAGE = (
    "---\ntype: concept\n---\n# Title\n\nOne-sentence summary: a page.\n\n"
    "## First\n\n" + "A paragraph of the first section. " * 12 + "\n\n"
    "### Inner\n\n" + "A paragraph of the inner section. " * 12 + "\n\n"
    "## Second\n\n" + "A paragraph of the second section. " * 12 + "\n"
)
SOURCES = {"knowledge/daily/2026-01-01.md": CONVERSATION, "knowledge/notes/a-page.md": PAGE}

# One pin per released version of the rule. A new version adds a line; an old line never changes.
PINNED = {
    "markdown-heading-extractor/v4": "bbb697b4db948a325ac3054a8befc3b78162d4ca2440c9d6e5c55785ea4efb3f",
}


def _chunk_fields(path: str, text: str) -> list:
    import corpus_snapshot

    content = text.encode("utf-8")
    chunks = corpus_snapshot.canonical_retrieval_chunks(
        source_id=f"source:{path}",
        source_path=path,
        source_sha256=hashlib.sha256(content).hexdigest(),
        content=content,
    )
    return [[chunk.byte_start, chunk.byte_end, list(chunk.heading_ancestry), chunk.span_sha256] for chunk in chunks]


def _chunking_digest() -> str:
    fields = {path: _chunk_fields(path, text) for path, text in sorted(SOURCES.items())}
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode("utf-8")).hexdigest()


def test_the_chunks_of_the_fixed_sources_are_the_ones_pinned_for_this_version():
    import corpus_snapshot

    assert PINNED.get(corpus_snapshot.EXTRACTOR_VERSION) == _chunking_digest()


def test_a_short_user_turn_that_states_a_fact_is_a_chunk_of_its_own():
    spans = [(start, end) for start, end, _ancestry, _sha in _chunk_fields(*next(iter(SOURCES.items())))]

    fact = CONVERSATION.encode("utf-8").index(b"**user:** I moved to Lisbon")
    assert fact in [start for start, _end in spans]
