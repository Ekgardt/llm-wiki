"""A step after the counted ones names its failure instead of ending the night.

Audit 2026-09-26, regress 8. docs/research/2026-09-26-a-housekeeping-step-names-its-failure.md
"""
from __future__ import annotations

import json

from tests.test_scheduled_fence import _state


def _raise(_log) -> None:
    raise TimeoutError("state lock busy")


def test_a_failed_update_is_named_and_the_night_still_succeeds(tmp_path, monkeypatch) -> None:
    import scheduled_nightly

    state_file = _state(tmp_path, monkeypatch)
    monkeypatch.setattr(scheduled_nightly, "_nightly_steps", lambda *_a, **_k: 0)
    monkeypatch.setattr(scheduled_nightly, "_prune_reports", lambda _log: None)
    monkeypatch.setattr(scheduled_nightly, "_update_code", _raise)

    code = scheduled_nightly._run_nightly_body(ownership=None)
    state = json.loads(state_file.read_text(encoding="utf-8"))
    report = "".join(path.read_text(encoding="utf-8") for path in (tmp_path / "logs").glob("nightly-*.md"))

    assert (code, state["last_nightly_status"], "update failed: " in report) == (0, "success", True)


def test_a_failed_prune_is_counted_and_the_update_still_runs(tmp_path, monkeypatch) -> None:
    import scheduled_nightly

    _state(tmp_path, monkeypatch)
    updated: list[bool] = []
    monkeypatch.setattr(scheduled_nightly, "_nightly_steps", lambda *_a, **_k: 0)
    monkeypatch.setattr(scheduled_nightly, "_prune_reports", _raise)
    monkeypatch.setattr(scheduled_nightly, "_update_code", lambda _log: updated.append(True))

    assert (scheduled_nightly._run_nightly_body(ownership=None), updated) == (1, [True])
