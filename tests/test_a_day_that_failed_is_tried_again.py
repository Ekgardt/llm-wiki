"""A day of sessions that failed part-way is resumed, not lost and not repeated.

The nightly took yesterday only, so five days that failed once were never read
again; a reply with a bracketed note was refused; a provider that returned
nothing read like a bad reply. Research:
`docs/research/2026-09-14-a-day-that-failed-is-tried-again.md`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import episode_consolidation as consolidation  # noqa: E402
from reply_json import reply_array  # noqa: E402

DAY = "2026-08-26"


def _vault(tmp_path: Path, records: int) -> Path:
    directory = tmp_path / "vault/knowledge/raw/sessions" / DAY
    directory.mkdir(parents=True)
    for index in range(records):
        (directory / f"s{index:03d}.md").write_text(f"# S{index}\n\nuser: line {index}\n", encoding="utf-8")
    return tmp_path / "vault"


def _state_after_failure(vault: Path, monkeypatch) -> dict:
    """Run a two-batch day whose second reply cannot be read; return the state it left."""
    saved: dict = {}
    monkeypatch.setattr(consolidation, "_save_progress", lambda day, p: saved.update({day: p.as_state()}))
    replies = iter(["[]", "not json at all"])
    outcome = consolidation.consolidate_day(vault, DAY, call=lambda _p: next(replies), state={})
    assert outcome["status"] == "partial"
    return {consolidation.PROGRESS_KEY: saved}


def test_a_rerun_skips_the_batch_that_already_finished(tmp_path, monkeypatch):
    vault = _vault(tmp_path, consolidation.MAX_RECORDS + 1)
    state = _state_after_failure(vault, monkeypatch)
    prompts: list[str] = []
    recorded: dict = {}
    monkeypatch.setattr(
        consolidation,
        "_record_consolidation",
        lambda d, c, r, digest: recorded.update(day=d, digest=digest),
    )

    outcome = consolidation.consolidate_day(
        vault, DAY, call=lambda prompt: prompts.append(prompt) or "[]", state=state
    )

    expected = {"day": DAY, "digest": consolidation.record_set_digest(vault, DAY)}
    assert (len(prompts), outcome["status"], recorded) == (1, "empty", expected)


def test_a_provider_that_returns_nothing_leaves_the_day_pending(tmp_path, monkeypatch):
    vault = _vault(tmp_path, 1)
    monkeypatch.setattr(consolidation, "_save_progress", lambda *a: pytest.fail("nothing finished"))

    with pytest.raises(consolidation.ConsolidationUnavailable):
        consolidation.consolidate_day(vault, DAY, call=lambda _p: None, state={})


def test_today_is_never_pending(tmp_path):
    vault = _vault(tmp_path, 1)

    assert consolidation.pending_days(vault, {}, today=DAY) == []


def test_a_bracketed_note_around_the_array_is_not_the_answer():
    items = [{"kind": "lesson", "text": "t", "quote": "q", "session": "s"}]
    reply = "See [[episode-notes]] first.\n" + json.dumps(items) + "\nFootnote [2]."

    assert reply_array(reply) == items
