"""Lost captures, the generation maintenance fence and recapture, claims and the generation scope as the doctor reports them."""

from __future__ import annotations

import json
from pathlib import Path

import generation_maintenance
import pytest

from tests.doctor_case_helpers import (
    _moment_days_ago,
    give_doctor_a_generous_budget,
)
from tests.doctor_helpers import (
    _adopt,
    _build_root,
    _check,
)

_budget_that_survives_a_slow_runner = pytest.fixture(autouse=True)(give_doctor_a_generous_budget)


def test_lost_captures_are_reported_as_a_degraded_capture_check(tmp_path):
    """A capture the hooks lost must show up in health, not only at session start."""
    import doctor

    root, state_root, home = _build_root(tmp_path)
    (state_root / "run").mkdir(parents=True, exist_ok=True)
    (state_root / "run" / "state.json").write_text(
        json.dumps(
            {
                "capture_failures": {
                    "session_end": {
                        "count": 2,
                        "last_reason": "spawn_failed",
                        "last_at": _moment_days_ago(0),
                    },
                    "pre_compact": {
                        "count": 1,
                        "last_reason": "spawn_failed",
                        "last_at": _moment_days_ago(0),
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    check = _check(doctor.run_doctor(root=root, state_root=state_root, home=home), "capture")

    assert check["status"] == "degraded"
    assert check["details"]["lost"] == 3
    assert check["details"]["kinds"] == {"session_end": 2, "pre_compact": 1}
    assert "capture-failures.jsonl" in check["details"]["trail"]
    del home


def test_a_loss_that_stopped_happening_returns_the_capture_check_to_green(tmp_path):
    """Nothing may require a human to make a report green again.

    The counter is evidence and stays; the finding covers the last seven days,
    so a vault whose loss was diagnosed and fixed recovers on its own.
    """
    import doctor

    root, state_root, home = _build_root(tmp_path)
    _adopt(root, state_root)
    (state_root / "run").mkdir(parents=True, exist_ok=True)
    (state_root / "run" / "state.json").write_text(
        json.dumps(
            {
                "capture_failures": {
                    "session_end": {
                        "count": 2,
                        "last_reason": "spawn_failed",
                        "last_at": _moment_days_ago(30),
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    check = _check(doctor.run_doctor(root=root, state_root=state_root, home=home), "capture")

    assert check["status"] == "ok"
    assert check["details"]["lost"] == 2
    assert check["details"]["live"] is False
    del home


def test_a_vault_without_lost_captures_reports_the_capture_check_as_ok(tmp_path):
    import doctor

    root, state_root, home = _build_root(tmp_path)
    _adopt(root, state_root)

    check = _check(doctor.run_doctor(root=root, state_root=state_root, home=home), "capture")

    assert check["status"] == "ok"
    assert check["details"]["lost"] == 0
    del home


def _stubbed_maintenance(monkeypatch, releases: list[object]) -> None:
    """One acquirable owner, and a record of every release of it."""
    monkeypatch.setattr(
        generation_maintenance, "acquire_maintenance_owner", lambda *a, **k: (object(), {"epoch": 1})
    )
    monkeypatch.setattr(
        generation_maintenance,
        "release_maintenance_owner",
        lambda coordinator, lease: releases.append(lease),
    )
    monkeypatch.setattr(generation_maintenance, "require_maintenance_owner", lambda *a, **k: None)
    monkeypatch.setattr(generation_maintenance, "heartbeat_maintenance_owner", lambda *a, **k: None)
    monkeypatch.setattr(generation_maintenance, "repair_generation_catalog", lambda *a, **k: None)
    monkeypatch.setattr(generation_maintenance, "unusable_filesystem_outcome", lambda *a, **k: None)


def test_a_vault_written_to_mid_capture_is_captured_again(tmp_path, monkeypatch):
    """An active vault changes while it is read; deferring would never refresh it."""
    import generation_maintenance
    from corpus_snapshot import CorpusChanged

    calls: list[bool] = []
    releases: list[object] = []

    def build(root, state, **kwargs):
        calls.append(kwargs["force_rebuild"])
        if len(calls) == 1:
            raise CorpusChanged("live corpus membership or source hashes changed")
        return {"status": "built", "generation_id": "gen-2"}

    _stubbed_maintenance(monkeypatch, releases)
    monkeypatch.setattr(generation_maintenance, "build_or_refresh_generation", build)

    result = generation_maintenance.run_generation_maintenance(tmp_path, tmp_path, time_budget_seconds=30)

    assert result["status"] == "built"
    assert calls == [False, True]
    assert len(releases) == 1, "the fence must outlive the recapture, not be released between"


def test_losing_the_maintenance_fence_is_reported_not_raised(tmp_path, monkeypatch):
    """The nightly timer and a manual run collide; the loser reports, not crashes."""
    import generation_maintenance

    def refresh(*_args, **_kwargs):
        raise RuntimeError("maintenance_owner_fence_lost")

    monkeypatch.setattr(generation_maintenance, "refreshed_generation", refresh)
    monkeypatch.setattr(
        generation_maintenance, "acquire_maintenance_owner", lambda *a, **k: (object(), object())
    )
    monkeypatch.setattr(generation_maintenance, "release_maintenance_owner", lambda *a, **k: None)
    monkeypatch.setattr(generation_maintenance, "unusable_filesystem_outcome", lambda *a, **k: None)

    result = generation_maintenance.run_generation_maintenance(tmp_path, tmp_path, time_budget_seconds=30)

    assert result["status"] == "deferred"
    assert result["reason"] == "maintenance_owner_lost"


def test_an_unrelated_runtime_error_still_escapes(tmp_path, monkeypatch):
    """Only the fence loss is an outcome; anything else is still a defect."""
    import generation_maintenance

    def refresh(*_args, **_kwargs):
        raise RuntimeError("something else went wrong")

    monkeypatch.setattr(generation_maintenance, "refreshed_generation", refresh)
    monkeypatch.setattr(
        generation_maintenance, "acquire_maintenance_owner", lambda *a, **k: (object(), object())
    )
    monkeypatch.setattr(generation_maintenance, "release_maintenance_owner", lambda *a, **k: None)
    monkeypatch.setattr(generation_maintenance, "unusable_filesystem_outcome", lambda *a, **k: None)

    with pytest.raises(RuntimeError, match="something else"):
        generation_maintenance.run_generation_maintenance(tmp_path, tmp_path, time_budget_seconds=30)


def test_losing_the_fence_during_the_recapture_is_also_reported(tmp_path, monkeypatch):
    """A fence genuinely lost during the second capture still reaches the report."""
    import generation_maintenance
    from corpus_snapshot import CorpusChanged

    calls: list[bool] = []
    releases: list[object] = []

    def build(root, state, **kwargs):
        calls.append(kwargs["force_rebuild"])
        if len(calls) == 1:
            raise CorpusChanged("live corpus membership or source hashes changed")
        raise RuntimeError("maintenance_owner_fence_lost")

    _stubbed_maintenance(monkeypatch, releases)
    monkeypatch.setattr(generation_maintenance, "build_or_refresh_generation", build)

    result = generation_maintenance.run_generation_maintenance(tmp_path, tmp_path, time_budget_seconds=30)

    assert result["status"] == "deferred"
    assert result["reason"] == "maintenance_owner_lost"
    assert calls == [False, True]


def test_a_second_change_during_the_recapture_defers_to_the_next_pass(tmp_path, monkeypatch):
    """Written to faster than it can be read: the next pass takes it."""
    import generation_maintenance
    from corpus_snapshot import CorpusChanged

    calls: list[bool] = []
    releases: list[object] = []

    def build(root, state, **kwargs):
        calls.append(kwargs["force_rebuild"])
        raise CorpusChanged("live corpus membership or source hashes changed")

    _stubbed_maintenance(monkeypatch, releases)
    monkeypatch.setattr(generation_maintenance, "build_or_refresh_generation", build)

    result = generation_maintenance.run_generation_maintenance(tmp_path, tmp_path, time_budget_seconds=30)

    assert result["status"] == "deferred"
    assert result["reason"] == "corpus_changed"
    assert calls == [False, True]


def test_the_host_markers_are_ones_the_shipped_templates_actually_write() -> None:
    """A marker naming a script the installer no longer writes fails every install."""
    from llm_wiki import (
        doctor_integrations,
    )

    root = Path(__file__).resolve().parent.parent
    shipped = {
        "claude": root / "integrations" / "claude-code" / "settings.json",
    }
    configs = doctor_integrations.integration_host_configs(Path.home())

    missing = {
        name: _absent_markers(configs[name], template.read_text(encoding="utf-8"))
        for name, template in shipped.items()
    }

    assert {name: absent for name, absent in missing.items() if absent} == {}


def _absent_markers(config: tuple, text: str) -> list[str]:
    _host_dir, files = config
    return [
        marker
        for _path, markers in files
        for marker in markers
        if marker not in text
    ]


def test_a_file_that_will_not_parse_is_named_not_treated_as_ill_health() -> None:
    """A refresh cannot fix a file the repository keeps deliberately broken."""
    from llm_wiki import (
        doctor_generation,
    )

    assert doctor_generation.generation_message(False, 0) == "Evidence generation is healthy."
    assert "1 file(s) did not parse" in doctor_generation.generation_message(False, 1)
    assert doctor_generation.generation_message(True, 0) == (
        "Evidence generation requires refresh."
    )
    assert doctor_generation.generation_message(True, 3) == (
        "Evidence generation requires refresh."
    )


def test_the_doctor_reads_the_z_suffix_this_runtime_writes():
    """Transactions and state are written with `Z`; a check must read them as UTC."""
    import generation_maintenance

    assert generation_maintenance.parse_utc("2026-08-22T00:00:00Z") == generation_maintenance.parse_utc("2026-08-22T00:00:00+00:00")


class _FixedScope:
    def __init__(self, scope: dict) -> None:
        self._scope = scope

    def as_dict(self) -> dict:
        return dict(self._scope)


_LIVE_SCOPE = {
    "schema_version": "repository-scope/v1",
    "repository_id": "repository:" + "a" * 64,
    "checkout_id": "checkout:" + "b" * 64,
    "checkout_root": "/repo",
    "git_common_dir": "/repo/.git",
    "git_commit": "c" * 40,
}


def test_a_generation_from_an_earlier_commit_is_superseded_not_mismatched():
    """`mismatched` reads as "this belongs to another repository". It did not.

    Comparing the whole scope made every commit report a mismatch, so the field
    was wrong far more often than right and said nothing when it mattered.
    """
    from llm_wiki import (
        doctor_generation,
    )

    scope = _FixedScope(_LIVE_SCOPE)
    earlier = {**_LIVE_SCOPE, "git_commit": "d" * 40}
    elsewhere = {**_LIVE_SCOPE, "checkout_id": "checkout:" + "e" * 64}

    assert doctor_generation.scope_state({"repository_scope": dict(_LIVE_SCOPE)}, scope) == "current"
    assert doctor_generation.scope_state({"repository_scope": earlier}, scope) == "superseded"
    assert doctor_generation.scope_state({"repository_scope": elsewhere}, scope) == "mismatched"
    assert doctor_generation.scope_state({}, scope) == "missing"
