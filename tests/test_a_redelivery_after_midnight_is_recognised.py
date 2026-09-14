"""An event written before midnight and delivered again after it is not written twice.

The marker was looked for in today's log only, and the transaction layer then refused
the operation as bound elsewhere, every retry. Research:
`docs/research/2026-09-14-a-redelivery-after-midnight-is-recognised.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def test_yesterdays_write_is_found_when_the_event_comes_again_today(tmp_path, monkeypatch):
    import daily_log_append

    daily = tmp_path / "knowledge" / "daily"
    (tmp_path / "knowledge" / "notes").mkdir(parents=True)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "runtime"))

    first = daily_log_append.locked_append_once(daily / "2026-07-13.md", "\nbody\n", "late-op")
    again = daily_log_append.locked_append_once(daily / "2026-07-14.md", "\nbody\n", "late-op")

    assert (first, again, (daily / "2026-07-14.md").exists()) == (True, False, False)
