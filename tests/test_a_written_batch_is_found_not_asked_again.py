"""A batch whose lessons reached the log is not asked again when its checkpoint was lost.

The block was written, the checkpoint save raised, and the next run asked the model
again and wrote a second block. Research:
`docs/research/2026-09-14-a-written-batch-is-found-not-asked-again.md`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import daily_log_append  # noqa: E402
import episode_consolidation as consolidation  # noqa: E402

DAY = "2026-08-26"
LATER = "2026-08-27"
REPLY = json.dumps([{"kind": "lesson", "text": "cron hides failures", "quote": "cron hides failures", "session": "s0"}])


def _vault(tmp_path: Path) -> Path:
    directory = tmp_path / "vault/knowledge/raw/sessions" / DAY
    directory.mkdir(parents=True)
    (directory / "s0.md").write_text("# S0\n\nuser: cron hides failures\n", encoding="utf-8")
    return tmp_path / "vault"


def _log_to(vault: Path, monkeypatch) -> Path:
    """The daily log a later night writes to, standing in for the transactional appender."""
    log = vault / "knowledge/daily" / f"{LATER}.md"
    log.parent.mkdir(parents=True)

    def append(slug, session_id, block, operation_id=None, **_kw):
        with log.open("a", encoding="utf-8") as handle:
            handle.write(block)
        return log

    monkeypatch.setattr(daily_log_append, "append_daily", append)
    return log


def test_a_lost_checkpoint_costs_no_second_call_and_no_second_block(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    log = _log_to(vault, monkeypatch)
    monkeypatch.setattr(consolidation, "_record_consolidation", lambda *a: None)

    def lost(day, progress):
        raise OSError("state lock timed out")

    monkeypatch.setattr(consolidation, "_save_progress", lost)
    with pytest.raises(OSError):
        consolidation.consolidate_day(vault, DAY, call=lambda _p: REPLY, state={})
    monkeypatch.setattr(consolidation, "_save_progress", lambda day, progress: None)
    calls: list[str] = []

    outcome = consolidation.consolidate_day(vault, DAY, call=lambda p: calls.append(p) or REPLY, state={})

    assert (calls, outcome["status"], log.read_text(encoding="utf-8").count("episodes | ")) == ([], "empty", 1)


def test_the_batch_marker_is_the_last_line_so_the_entry_keeps_its_id(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    log = _log_to(vault, monkeypatch)
    monkeypatch.setattr(consolidation, "_record_consolidation", lambda *a: None)
    monkeypatch.setattr(consolidation, "_save_progress", lambda day, progress: None)

    consolidation.consolidate_day(vault, DAY, call=lambda _p: REPLY, state={})
    lines = log.read_text(encoding="utf-8").splitlines()

    assert (lines[0].startswith("- `["), lines[-1].startswith("<!-- llm-wiki-episode-batch:")) == (True, True)
