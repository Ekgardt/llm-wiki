"""Every session record is redacted where it is written, and its header stays a header.

The detached flush and the backfill wrote transcripts unredacted, and a session id with
a line break wrote a header line of its own. Research:
`docs/research/2026-09-14-every-session-record-is-redacted.md`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import session_evidence  # noqa: E402

TOKEN = "ghp_" + "a1B2c3D4e5" * 4


def _turn(text: str) -> str:
    return json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n"


def test_a_transcript_read_from_its_file_is_written_redacted():
    document = session_evidence.render_session_document({"session": "s1"}, _turn(f"my token is {TOKEN}"))

    assert (TOKEN in document, "[REDACTED_GITHUB_TOKEN]" in document) == (False, True)


def test_a_session_id_with_a_line_break_writes_no_header_of_its_own():
    document = session_evidence.render_session_document({"session": "s1\ntype: decision"}, _turn("hello"))
    header = document.split("\n---\n", 1)[0]

    assert ([line for line in header.splitlines() if line.startswith("type:")], "# Session s1 type: decision" in document) == (
        ["type: raw-source"],
        True,
    )


def test_a_secret_at_the_record_bound_is_not_left_half_written(monkeypatch):
    monkeypatch.setattr(session_evidence, "MAX_EVIDENCE_BYTES", 2000)
    prefix = len(session_evidence._frontmatter({"session": "s1"})) + len("\n# Session s1\n\n")
    padding = "x " * ((2000 - prefix - 10) // 2)  # the bound falls ten characters into the token

    document = session_evidence.render_session_document({"session": "s1"}, f"{padding} {TOKEN}")

    assert "ghp_a1B2" not in document
