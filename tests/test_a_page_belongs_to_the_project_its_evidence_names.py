"""A compiled page belongs to the project its quoted blocks name (audit 2026-09-26 B-14).

docs/research/2026-09-26-a-page-belongs-to-the-project-its-evidence-names.md
"""
from __future__ import annotations

from datetime import datetime, timezone

import compile_memory
import flush_memory

RECORD = {"event": "session_end", "session": "s1", "trigger": "session-end", "host": "claude", "intent_id": "a" * 64}


def _block(project: str | None) -> bytes:
    record = {**RECORD, "project_slug": project}
    return flush_memory._capture_daily_block(record, "major", "## Decisions made\n- x\n", datetime.now(timezone.utc)).encode()


def test_a_captured_block_names_its_project() -> None:
    assert b"- Project slug: `llm-wiki`\n" in _block("llm-wiki")


def test_one_project_across_the_evidence_is_the_page_project() -> None:
    operation = compile_memory._with_page_project({}, [_block("llm-wiki"), _block("llm-wiki")])

    assert compile_memory._project_line(operation) == "project: llm-wiki\n"


def test_mixed_or_unnamed_evidence_leaves_the_page_global() -> None:
    mixed = compile_memory._with_page_project({}, [_block("llm-wiki"), _block("other")])
    unnamed = compile_memory._with_page_project({}, [_block("llm-wiki"), _block(None)])

    assert (mixed, unnamed) == ({}, {})
