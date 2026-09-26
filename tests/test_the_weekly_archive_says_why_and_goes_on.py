"""The weekly archive names why an old day stays flat, and one failure does not end it.

See docs/research/2026-09-25-the-weekly-archive-says-why-and-goes-on.md.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import archive_daily
import pytest

DAYS = [Path(f"/v/knowledge/daily/2026-04-{day:02d}.md") for day in (13, 19, 20)]


@pytest.fixture
def archiver(monkeypatch):
    checks = {
        "2026-04-13": SimpleNamespace(eligible=False, reasons=("nonterminal_compile_operation", "decision_evidence")),
        "2026-04-20": SimpleNamespace(eligible=False, reasons=("hot_retention",)),
    }

    def eligible(source, **_kwargs):
        if source.stem == "2026-04-19":
            raise RuntimeError("receipt transaction disappeared")
        return checks[source.stem]

    fake = SimpleNamespace(eligible=eligible)
    monkeypatch.setattr(archive_daily, "DailyArchiver", lambda root, state_root: fake)
    monkeypatch.setattr(archive_daily, "_flat_daily_sources", lambda _archiver: DAYS)
    return fake


def test_an_old_day_says_why_a_failure_is_named_and_the_run_goes_on(archiver, capsys) -> None:
    status = archive_daily.main(["--commit"])
    out, err = capsys.readouterr()

    assert (
        status,
        "Kept flat: 2026-04-13: nonterminal_compile_operation, decision_evidence" in out,
        "2026-04-20" in out,
        "Failed: 2026-04-19: RuntimeError: receipt transaction disappeared" in err,
        "Archived 0 log(s); 1 failed." in out,
    ) == (1, True, False, True, True)
