"""Tests for feedback_capture.py — correction/preference detection."""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def test_detect_correction():
    """'No, use X instead' → correction."""
    import feedback_capture
    ftype, conf = feedback_capture._detect_feedback_type(
        "No, we should use JWT instead of sessions because stateless"
    )
    assert ftype == "correction"
    assert conf >= 0.5


def test_detect_preference():
    """'I prefer concise answers' → preference."""
    import feedback_capture
    ftype, conf = feedback_capture._detect_feedback_type(
        "I prefer concise answers without unnecessary preamble"
    )
    assert ftype == "preference"
    assert conf >= 0.5


def test_detect_instruction():
    """'Remember that we use Postgres' → instruction."""
    import feedback_capture
    ftype, conf = feedback_capture._detect_feedback_type(
        "Remember that we always use Postgres for the database layer"
    )
    assert ftype in ("instruction", "preference")
    assert conf >= 0.5


def test_detect_rejection():
    """'That's not right' → rejection or correction (both are valid feedback)."""
    import feedback_capture
    ftype, conf = feedback_capture._detect_feedback_type(
        "That's not right, the auth middleware should validate first"
    )
    assert ftype in ("rejection", "correction")  # "not" matches both patterns
    assert conf >= 0.5


def test_detect_noise_filtered():
    """'ok thanks' → None (noise)."""
    import feedback_capture
    ftype, conf = feedback_capture._detect_feedback_type("ok thanks")
    assert ftype is None
    assert conf == 0.0


def test_detect_short_text_filtered():
    """Text < 10 chars → None."""
    import feedback_capture
    ftype, conf = feedback_capture._detect_feedback_type("hi")
    assert ftype is None


def test_detect_empty_text():
    """Empty string → None."""
    import feedback_capture
    assert feedback_capture._detect_feedback_type("")[0] is None
    assert feedback_capture._detect_feedback_type(None)[0] is None


def test_detect_multiple_patterns_higher_confidence():
    """Multiple pattern matches → higher confidence."""
    import feedback_capture
    # Contains both "no" and "should" and "must"
    ftype, conf = feedback_capture._detect_feedback_type(
        "No, this must be changed, we should always validate inputs"
    )
    assert conf >= 0.7  # multiple patterns boost confidence


def test_capture_from_text_saves_candidate(tmp_path, monkeypatch):
    """Valid correction saves a JSON candidate file."""
    import feedback_capture

    monkeypatch.setattr(feedback_capture, "FEEDBACK_DIR", tmp_path)
    cid = feedback_capture.capture_from_text(
        "No, we should use JWT instead of sessions because stateless matters",
        session_id="test-session",
        slug="test-project",
        trigger="test",
    )
    assert cid is not None
    candidate_file = tmp_path / f"{cid}.json"
    assert candidate_file.exists()
    candidate = json.loads(candidate_file.read_text())
    assert candidate["type"] == "correction"
    assert candidate["status"] == "candidate"
    assert candidate["session_id"] == "test-session"


def test_capture_from_text_rejects_noise(tmp_path, monkeypatch):
    """Noise text returns None, no file saved."""
    import feedback_capture

    monkeypatch.setattr(feedback_capture, "FEEDBACK_DIR", tmp_path)
    cid = feedback_capture.capture_from_text("ok cool thanks")
    assert cid is None
    assert len(list(tmp_path.glob("*.json"))) == 0


def test_list_candidates(tmp_path, monkeypatch):
    """list_candidates returns only candidates with matching status."""
    import feedback_capture

    monkeypatch.setattr(feedback_capture, "FEEDBACK_DIR", tmp_path)
    feedback_capture.capture_from_text(
        "No, use JWT instead", session_id="s1", slug="p1", trigger="t"
    )
    candidates = feedback_capture.list_candidates("candidate")
    assert len(candidates) == 1


def test_promote_candidate_creates_knowledge_page(tmp_path, monkeypatch):
    """Promoting a candidate creates a knowledge .md file."""
    import feedback_capture

    # Create a candidate first
    monkeypatch.setattr(feedback_capture, "FEEDBACK_DIR", tmp_path / "feedback")
    feedback_capture.FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
    cid = feedback_capture.capture_from_text(
        "No, use JWT instead of sessions because stateless",
        session_id="s1", slug="p1", trigger="test",
    )

    # Promote it
    knowledge_dir = tmp_path / "knowledge" / "patterns"
    knowledge_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(feedback_capture, "ROOT", tmp_path)

    result = feedback_capture.promote_candidate(cid, "patterns")
    assert result is not None
    assert result.endswith(".md")

    # Candidate status updated
    candidate_file = tmp_path / "feedback" / f"{cid}.json"
    candidate = json.loads(candidate_file.read_text())
    assert candidate["status"] == "promoted"
