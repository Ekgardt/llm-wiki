"""The session-start advice about the classifier reads the decisions it made.

It read a counter only the retired hook path wrote: 74/74 FLUSH_OK, while the live
decisions were 23 ok, 22 major, 11 minor. Research:
`docs/research/2026-09-14-the-advice-reads-the-decisions.md`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import session_start_context  # noqa: E402


def _decisions(state_root: Path, tiers: list[str]) -> None:
    results = state_root / "run" / "queue-results"
    results.mkdir(parents=True)
    for index, tier in enumerate(tiers):
        (results / f"capture-decision-{index:04d}.json").write_text(json.dumps({"tier": tier}), encoding="utf-8")
    (results / "capture-decision-broken.json").write_text("not json", encoding="utf-8")


def test_a_classifier_that_keeps_most_sessions_is_not_called_too_strict(tmp_path):
    _decisions(tmp_path, ["ok"] * 23 + ["major"] * 22 + ["minor"] * 11)

    counts = session_start_context.capture_tier_counts(tmp_path)

    assert (counts, session_start_context._flush_line(counts)) == ({"ok": 23, "major": 22, "minor": 11}, "")


def test_a_classifier_that_keeps_almost_nothing_is_still_flagged(tmp_path):
    _decisions(tmp_path, ["ok"] * 9 + ["major"])

    line = session_start_context._flush_line(session_start_context.capture_tier_counts(tmp_path))

    assert "9/10 sessions returned FLUSH_OK" in line
