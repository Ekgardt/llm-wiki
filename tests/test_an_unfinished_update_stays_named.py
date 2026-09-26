"""What an earlier update left undone stays named until it is done (audit 2026-09-26 B-24).

docs/research/2026-09-26-an-unfinished-update-stays-named.md
"""
from __future__ import annotations

import json

import scheduled_nightly
import self_update

CURRENT = {"status": "current", "reason": None, "commit": "abc"}


def test_stale_dependencies_are_synced_again_on_a_quiet_night(monkeypatch) -> None:
    monkeypatch.setattr(self_update, "sync_dependencies", lambda _root: "synced")

    outcome = scheduled_nightly._carried_forward(dict(CURRENT), {"dependencies": "stale", "at": "2026-09-20T03:00:00+00:00"})

    assert outcome["dependencies"] == "synced"


def test_an_installer_run_stays_asked_for_until_it_happens(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(scheduled_nightly, "STATE_ROOT", tmp_path)
    previous = {"resources": "rerun_installer", "at": "2026-09-20T03:00:00+00:00"}

    kept = scheduled_nightly._carried_forward(dict(CURRENT), previous)
    (tmp_path / "run" / "install").mkdir(parents=True)
    (tmp_path / "run" / "install" / "manifest.json").write_text(json.dumps({"committed_at": "2026-09-21T10:00:00Z"}))
    cleared = scheduled_nightly._carried_forward(dict(CURRENT), kept)

    assert (kept.get("resources"), cleared.get("resources")) == ("rerun_installer", None)


def test_the_advice_keeps_the_extras() -> None:
    import doctor

    advice = doctor._UPDATE_ATTENTION[("dependencies", "stale")]

    assert "--inexact" in advice
