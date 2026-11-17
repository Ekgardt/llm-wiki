"""Fallback capture must retain UTF-8 text and its physical CRLF endings."""
from pathlib import Path

import pytest

from tests import test_a_pending_codex_tail_is_not_a_cache_entry as tail_case
from tests.a_codex_turn_is_not_a_session_helpers import codex as codex
from tests.adopted_capture_vault import host_transcript, intent_records
from tests.historical_test_protocols_keep_their_exact_utf8_bytes_helpers import _windows_text_write

TEXT = "\u041f\u043e\u043b\u043d\u044b\u0439 \u0440\u0430\u0437\u0433\u043e\u0432\u043e\u0440 \u043e\u0441\u0442\u0430\u0451\u0442\u0441\u044f \u0441\u043e\u0445\u0440\u0430\u043d\u0451\u043d\u043d\u044b\u043c.\nThe complete conversation remains durable.\n"


def _utf8_transcript(state_root, name, ignored):
    return host_transcript(state_root, name, TEXT)


def _windows_utf8_fixture(monkeypatch):
    monkeypatch.setattr(Path, "write_text", _windows_text_write)
    monkeypatch.setattr(tail_case, "host_transcript", _utf8_transcript)


def _rewritten_capture(state_root):
    records = intent_records(state_root)
    part = records[0]["evidence"][0]["parts"][0]
    part["text"] = part["text"].replace("\r\n", "\n")
    return records


def test_complete_fallback_capture_preserves_physical_crlf_and_utf8(codex, monkeypatch):
    _windows_utf8_fixture(monkeypatch)
    tail_case.test_refused_tail_plan_still_publishes_a_complete_durable_capture(codex, monkeypatch)


def test_fallback_capture_guard_detects_rewritten_line_endings(codex, monkeypatch):
    _windows_utf8_fixture(monkeypatch)
    monkeypatch.setattr(tail_case, "intent_records", _rewritten_capture)
    with pytest.raises(AssertionError):
        tail_case.test_refused_tail_plan_still_publishes_a_complete_durable_capture(codex, monkeypatch)
