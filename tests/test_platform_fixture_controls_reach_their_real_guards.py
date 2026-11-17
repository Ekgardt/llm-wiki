"""A forged journal frame read with a Windows encoding still reaches the unchanged authority guard."""
from pathlib import Path

from tests import test_native_user_frames_keep_their_physical_evidence as native_cases


def _windows_text_read(path, encoding=None, errors=None):
    with path.open("r", encoding=encoding or "cp1252", errors=errors) as source:
        return source.read()


def test_forged_frame_control_reaches_authority_guard_with_windows_encoding(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "read_text", _windows_text_read)
    native_cases.test_forged_journal_framing_grants_no_daily_fact(
        tmp_path, "## [01:00:00] Captured event")
