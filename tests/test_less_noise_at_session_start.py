"""Session start carries no word-match impact guesses, no half words, and leaves its debug copy.

Research: `docs/research/2026-09-14-less-noise-at-session-start.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def test_word_match_guesses_are_not_injected_at_session_start():
    from impact_analysis import format_for_advisory

    impact = {
        "summary": "1 diff record(s), 0 canonical symbol(s), 0 affected artifact(s).",
        "stale_pages": [
            {"slug": "Andrej Karpathy", "confidence": "low", "reason": "mentions words", "method": "textual-name-match"},
        ],
    }

    assert format_for_advisory(impact) == ""


def test_a_long_summary_ends_on_a_whole_word():
    import build_guardrails

    text = "Decided that the slow-transaction logging threshold must be derived from research rather than picked, because a threshold set too low is worse than none"

    clipped = build_guardrails._clipped(text)

    assert (len(clipped) <= build_guardrails.SUMMARY_MAX_CHARS, clipped.endswith("…"), text.startswith(clipped[:-1])) == (True, True, True)
    assert text[len(clipped) - 1] == " "


def test_the_hook_leaves_its_payload_where_the_docs_say(tmp_path, monkeypatch):
    import integration_adapter
    import session_start_context

    monkeypatch.setattr(session_start_context, "DEBUG_DIR", tmp_path / "logs")
    monkeypatch.setattr(session_start_context, "DEBUG_FILE", tmp_path / "logs" / "session-start-last.txt")
    monkeypatch.setattr(session_start_context, "latest_daily", lambda: None)

    integration_adapter._write_session_start_debug("# Project memory context")

    assert "# Project memory context" in (tmp_path / "logs" / "session-start-last.txt").read_text(encoding="utf-8")
