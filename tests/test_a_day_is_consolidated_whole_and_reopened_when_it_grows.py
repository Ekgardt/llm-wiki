"""No day is cut at 240 records, and a day that grows after closing is read again.

The leftovers of finding M-A13 of the third audit. See
`docs/research/2026-09-17-a-day-is-consolidated-whole-and-reopened-when-it-grows.md`
and `docs/research/2026-09-18-the-day-a-late-record-reopens.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import episode_consolidation as consolidation  # noqa: E402

DAY = "2026-08-23"
RECORD = "---\ntype: raw-source\n---\n\n# Session {name}\n\n**assistant:** it works\n"
ONE_RUN = consolidation.MAX_RECORDS * consolidation.MAX_BATCHES_PER_RUN


def _vault(tmp_path: Path, count: int) -> Path:
    directory = tmp_path / "knowledge/raw/sessions" / DAY
    directory.mkdir(parents=True)
    for index in range(count):
        name = f"s{index:04d}"
        (directory / f"{name}.md").write_text(RECORD.format(name=name), encoding="utf-8")
    return tmp_path


def test_a_day_larger_than_one_run_keeps_all_of_its_records(tmp_path: Path) -> None:
    """The ceiling used to drop every record past the 240th, for ever."""
    vault = _vault(tmp_path, ONE_RUN + 5)

    records = consolidation.session_records(vault, DAY)

    assert len(records) == ONE_RUN + 5


def test_one_run_attempts_no_more_than_its_share_and_the_day_stays_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path, ONE_RUN + consolidation.MAX_RECORDS * 2)
    calls: list[str] = []
    monkeypatch.setattr(consolidation, "_save_progress", lambda day, progress: None)
    monkeypatch.setattr(
        consolidation, "_record_consolidation", lambda *args: pytest.fail("not whole yet")
    )
    monkeypatch.setattr(consolidation, "_write_block", lambda *args: Path("daily.md"))
    monkeypatch.setattr(
        consolidation,
        "grounded_lessons",
        lambda raw, paths: [consolidation.Lesson("lesson", "t", "q", "s")],
    )

    outcome = consolidation.consolidate_day(
        vault, DAY, call=lambda prompt: calls.append(prompt) or "[]", state={}
    )

    assert (outcome["status"], len(calls)) == (
        "partial",
        consolidation.MAX_BATCHES_PER_RUN,
    )


def test_a_record_that_arrives_after_the_day_closed_reopens_it(tmp_path: Path) -> None:
    vault = _vault(tmp_path, 1)
    state = {
        "consolidated_session_days": {
            DAY: {
                "items": 1,
                "records": 1,
                "record_set": consolidation.record_set_digest(vault, DAY),
            }
        }
    }
    closed = consolidation.pending_days(vault, state, today="2026-08-24")

    (vault / "knowledge/raw/sessions" / DAY / "late.md").write_text(
        RECORD.format(name="late"), encoding="utf-8"
    )

    assert (closed, consolidation.pending_days(vault, state, today="2026-08-24")) == (
        [],
        [DAY],
    )


def test_a_day_closed_before_the_digest_existed_stays_closed(tmp_path: Path) -> None:
    """Re-reading the whole imported history would cost a call for every batch."""
    vault = _vault(tmp_path, 2)
    state = {"consolidated_session_days": {DAY: {"items": 0, "records": 1}}}

    outcome = consolidation.consolidate_day(
        vault, DAY, call=lambda _prompt: pytest.fail("no call"), state=state
    )

    assert outcome["reason"] == "already_consolidated"
    assert consolidation.pending_days(vault, state, today="2026-08-24") == []
