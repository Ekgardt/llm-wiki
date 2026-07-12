"""Tests for session_feedback.py — decision honored/contradicted tracking."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


class TestRecordAndGetStaleness:
    """Test staleness scoring."""

    def test_fresh_decision_has_zero_score(self, tmp_path, monkeypatch):
        import session_feedback

        monkeypatch.setattr(session_feedback, "FEEDBACK_FILE", tmp_path / "fb.json")
        assert session_feedback.get_staleness_score("fresh-decision") == 0.0

    def test_should_inject_fresh(self, tmp_path, monkeypatch):
        import session_feedback

        monkeypatch.setattr(session_feedback, "FEEDBACK_FILE", tmp_path / "fb.json")
        assert session_feedback.should_inject("any-decision") is True

    def test_contradicted_increases_score(self, tmp_path, monkeypatch):
        import session_feedback

        fb = tmp_path / "fb.json"
        monkeypatch.setattr(session_feedback, "FEEDBACK_FILE", fb)
        session_feedback.update_decision_staleness("test", "contradicted")
        assert session_feedback.get_staleness_score("test") > 0.0

    def test_honored_decreases_score(self, tmp_path, monkeypatch):
        import session_feedback

        fb = tmp_path / "fb.json"
        monkeypatch.setattr(session_feedback, "FEEDBACK_FILE", fb)
        # First contradict, then honor.
        session_feedback.update_decision_staleness("test", "contradicted")
        session_feedback.update_decision_staleness("test", "honored")
        assert session_feedback.get_staleness_score("test") < 0.3

    def test_stale_decision_not_injected(self, tmp_path, monkeypatch):
        import session_feedback

        fb = tmp_path / "fb.json"
        monkeypatch.setattr(session_feedback, "FEEDBACK_FILE", fb)
        # Contradict multiple times to push above threshold.
        for _ in range(4):
            session_feedback.update_decision_staleness("stale", "contradicted")
        assert session_feedback.should_inject("stale") is False

    def test_unknown_no_change(self, tmp_path, monkeypatch):
        import session_feedback

        fb = tmp_path / "fb.json"
        monkeypatch.setattr(session_feedback, "FEEDBACK_FILE", fb)
        session_feedback.update_decision_staleness("test", "unknown")
        assert session_feedback.get_staleness_score("test") == 0.0


class TestRecordInjection:
    """Test injection recording."""

    def test_record_injection(self, tmp_path, monkeypatch):
        import session_feedback

        fb = tmp_path / "fb.json"
        monkeypatch.setattr(session_feedback, "FEEDBACK_FILE", fb)
        session_feedback.record_injection("auth-decision", "session-1")
        data = json.loads(fb.read_text(encoding="utf-8"))
        assert len(data["injections"]) == 1
        assert data["injections"][0]["slug"] == "auth-decision"


class TestCheckSession:
    """Test session correction detection."""

    def test_no_daily_returns_unknown(self, tmp_path, monkeypatch):
        import session_feedback

        monkeypatch.setattr(session_feedback, "DAILY_DIR", tmp_path / "no_daily")
        result = session_feedback.check_session_for_corrections("test")
        assert result == "unknown"

    def test_detects_correction(self, tmp_path, monkeypatch):
        from datetime import datetime

        import session_feedback

        daily = tmp_path / "daily"
        daily.mkdir()
        today = datetime.now().strftime("%Y-%m-%d")
        (daily / f"{today}.md").write_text(
            "## [10:00:00] session-end\n"
            "slug: test-decision\n"
            "Actually, we should not use that approach anymore.\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(session_feedback, "DAILY_DIR", daily)

        result = session_feedback.check_session_for_corrections("test-decision")
        assert result == "contradicted"

    def test_detects_honored(self, tmp_path, monkeypatch):
        from datetime import datetime

        import session_feedback

        daily = tmp_path / "daily"
        daily.mkdir()
        today = datetime.now().strftime("%Y-%m-%d")
        (daily / f"{today}.md").write_text(
            "## [10:00:00] session-end\n"
            "slug: test-decision\n"
            "Continued using the approach as planned.\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(session_feedback, "DAILY_DIR", daily)

        result = session_feedback.check_session_for_corrections("test-decision")
        assert result == "honored"
