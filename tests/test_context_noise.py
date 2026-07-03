"""Regression test: session_start_context strips machine-specific noise (Round 3 #I4).

Injected additionalContext MUST NOT include:
  - `Trigger: ...`  (hook metadata)
  - `Transcript: ...` (local filesystem paths)
  - `Project root: ...` (absolute paths, machine-specific)
  - Session-end header UUIDs (literal session IDs)

Useful signal must survive:
  - `# Session Memory Index` header
  - Wikilinks into memory/knowledge/
  - `Project slug: ...` (project identity, useful)
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "session_start_context.py"


@pytest.fixture(scope="module")
def injected_context() -> str:
    # conftest.py bootstraps LLM_WIKI_ROOT and LLM_WIKI_STATE_ROOT in
    # os.environ; subprocess inherits. The script otherwise depends on
    # memory_state.ROOT (script-file-relative), which works regardless,
    # so this subprocess is safe even without env vars — but tests that
    # DO rely on env stay consistent.
    import os
    out = subprocess.check_output(
        [sys.executable, str(SCRIPT)],
        env=os.environ.copy(),
        text=True,
    )
    d = json.loads(out)
    return d["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize(
    "forbidden",
    [
        "- Trigger:",
        "- Transcript:",
        "- Project root:",
        r"C:\Users\\",
    ],
)
def test_noise_stripped(injected_context: str, forbidden: str):
    assert forbidden not in injected_context, (
        f"injected context still contains forbidden fragment: {forbidden!r}"
    )


def test_no_session_uuid(injected_context: str):
    """Session-end headers should have their `| <uuid>` tail trimmed."""
    uuid_re = re.compile(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
    )
    assert not uuid_re.search(injected_context), (
        "UUID found in injected context — session-id strip regex regressed"
    )


def test_useful_signal_preserved(injected_context: str):
    """Context should still carry navigable links and index header."""
    # index-derived content
    assert "Session Memory Index" in injected_context
    # at least one wikilink into the knowledge tree
    assert "[[memory/knowledge/" in injected_context


def test_context_size_reasonable(injected_context: str):
    """Sanity: injected context fits in a reasonable budget (≤ 4 KB)."""
    assert 0 < len(injected_context) <= 4000, (
        f"injected context size outside expected range: {len(injected_context)} chars"
    )
