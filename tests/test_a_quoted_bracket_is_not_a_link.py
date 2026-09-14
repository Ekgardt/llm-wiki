"""Quoted code with `[[` must not become a link, nor kill the generation build.

LongMemEval questions `4100d0a0` and `28dc39ac` failed on every run: a session
quoted pandas `df[["Latitude", "Longitude", ...` and a `]]` came turns later, the
extractor read everything between as one wikilink target, and the writer refused
a target with line breaks. Research:
`docs/research/2026-09-14-a-link-lives-on-one-line.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from tests.test_knowledge_extractor import _source  # noqa: E402

QUOTED = (
    b"---\ntype: concept\n---\n"
    b'```python\ndf = data[["Latitude", "Longitude",\n```\n\n'
    b"**user:** and then\n\nresult = grid[0]]\n\nsee [[b]] and [[ ]]\n"
)


def _extracted():
    from knowledge_extractor import extract_knowledge

    first = _source("knowledge/notes/a.md", QUOTED)
    second = _source("knowledge/notes/b.md", b"---\ntype: concept\n---\n# B\n")
    return extract_knowledge((first, second))


def test_no_observed_target_carries_a_line_break_or_is_empty():
    targets = [row["target_text"] for row in _extracted().observations]

    assert [target for target in targets if not target or "\n" in target] == []


def test_a_real_link_after_quoted_brackets_still_resolves():
    edges = [row for row in _extracted().assertions if row["edge_type"] == "LINKS_TO"]

    assert len(edges) == 1
