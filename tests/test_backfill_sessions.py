"""MEM-08: past transcripts become session records, once, on the operator's word."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import backfill_sessions  # noqa: E402

TRANSCRIPT = (
    '{"type":"user","message":{"role":"user","content":"why systemd and not cron?"}}\n'
    '{"type":"assistant","message":{"role":"assistant","content":'
    '[{"type":"text","text":"A user timer survives a reboot."}]}}\n'
)


def _vault(tmp_path: Path, monkeypatch) -> Path:
    vault = tmp_path / "vault"
    (vault / "knowledge/raw").mkdir(parents=True)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(vault))
    return vault


def _transcript(tmp_path: Path, name: str, text: str, *, day: str = "2026-08-01") -> Path:
    source = tmp_path / "projects" / "demo"
    source.mkdir(parents=True, exist_ok=True)
    path = source / f"{name}.jsonl"
    path.write_text(text, encoding="utf-8")
    stamp = time.mktime(time.strptime(f"{day} 12:00:00", "%Y-%m-%d %H:%M:%S"))
    os.utime(path, (stamp, stamp))
    return path


def test_the_plan_writes_nothing_and_says_what_it_would_write(tmp_path, monkeypatch):
    vault = _vault(tmp_path, monkeypatch)
    _transcript(tmp_path, "s1", TRANSCRIPT)

    outcome = backfill_sessions.backfill(
        vault, (tmp_path / "projects",), apply=False
    )

    assert outcome.scanned == 1
    assert outcome.written == 1
    assert outcome.bytes_written > 0
    assert list((vault / "knowledge/raw").rglob("*.md")) == []


def test_a_record_is_written_under_the_day_the_session_ended(tmp_path, monkeypatch):
    vault = _vault(tmp_path, monkeypatch)
    _transcript(tmp_path, "s1", TRANSCRIPT, day="2026-08-01")

    outcome = backfill_sessions.backfill(vault, (tmp_path / "projects",), apply=True)

    record = vault / "knowledge/raw/sessions/2026-08-01/s1.md"
    assert outcome.written == 1
    assert record.exists()
    text = record.read_text(encoding="utf-8")
    assert "why systemd and not cron?" in text
    assert "source_authority: session" in text


def test_running_it_twice_writes_the_record_once(tmp_path, monkeypatch):
    vault = _vault(tmp_path, monkeypatch)
    _transcript(tmp_path, "s1", TRANSCRIPT)

    first = backfill_sessions.backfill(vault, (tmp_path / "projects",), apply=True)
    second = backfill_sessions.backfill(vault, (tmp_path / "projects",), apply=True)

    assert (first.written, first.present) == (1, 0)
    assert (second.written, second.present) == (0, 1)


def test_a_transcript_with_no_conversation_is_not_kept(tmp_path, monkeypatch):
    vault = _vault(tmp_path, monkeypatch)
    _transcript(tmp_path, "empty", '{"type":"system","content":"noise"}\n')

    outcome = backfill_sessions.backfill(vault, (tmp_path / "projects",), apply=True)

    assert (outcome.written, outcome.empty) == (0, 1)
    assert list((vault / "knowledge/raw").rglob("*.md")) == []


def test_a_refused_write_is_counted_and_named(tmp_path, monkeypatch):
    """The DLP boundary may refuse a record; the count must say so, not hide it."""
    vault = _vault(tmp_path, monkeypatch)
    _transcript(tmp_path, "secretish", TRANSCRIPT)
    monkeypatch.setattr(
        backfill_sessions, "write_session_evidence", lambda *args, **kwargs: None
    )

    outcome = backfill_sessions.backfill(vault, (tmp_path / "projects",), apply=True)

    assert outcome.refused == 1
    assert outcome.refused_sessions == ["secretish"]
