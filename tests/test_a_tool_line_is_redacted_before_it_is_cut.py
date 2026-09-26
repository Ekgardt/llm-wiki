"""A tool line is redacted before it is cut, so a split secret does not leak (audit 2026-09-26 C-6).

docs/research/2026-09-26-a-tool-line-is-redacted-before-it-is-cut.md
"""
from __future__ import annotations

import session_evidence

TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # gitleaks:allow — invented token shape


def test_a_token_across_the_bound_leaves_nothing_of_itself() -> None:
    command = "x" * (session_evidence.MAX_TOOL_LINE_CHARS - 10) + " " + TOKEN
    block = {"name": "Bash", "input": {"command": command}}

    line = session_evidence._tool_line(block)

    assert "ghp_" not in line
