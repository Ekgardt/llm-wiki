from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


def test_failed_nightly_releases_claim_and_records_failure(tmp_path, monkeypatch):
    import memory_state
    import scheduled_nightly

    monkeypatch.setattr(memory_state, "STATE_DIR", tmp_path / "run")
    monkeypatch.setattr(memory_state, "STATE_FILE", tmp_path / "run" / "state.json")
    monkeypatch.setattr(memory_state, "LOCK_FILE", tmp_path / "run" / "state.json.lock")
    monkeypatch.setattr(scheduled_nightly, "update_state", memory_state.update_state)
    memory_state.save_state({
        "nightly_catchup_claim": {
            "date": "2026-07-12", "status": "claimed", "expires_at": "2999-01-01T00:00:00"
        }
    })

    scheduled_nightly._record_nightly_result("2026-07-12", failures=1)

    state = json.loads(memory_state.STATE_FILE.read_text(encoding="utf-8"))
    assert "nightly_catchup_claim" not in state
    assert state["last_nightly_status"] == "failed"
    assert state["last_nightly_failure"]["date"] == "2026-07-12"
    assert "last_nightly_date" not in state


def test_successful_nightly_releases_claim_and_records_completion(tmp_path, monkeypatch):
    import memory_state
    import scheduled_nightly

    monkeypatch.setattr(memory_state, "STATE_DIR", tmp_path / "run")
    monkeypatch.setattr(memory_state, "STATE_FILE", tmp_path / "run" / "state.json")
    monkeypatch.setattr(memory_state, "LOCK_FILE", tmp_path / "run" / "state.json.lock")
    monkeypatch.setattr(scheduled_nightly, "update_state", memory_state.update_state)
    memory_state.save_state({"nightly_catchup_claim": {"date": "2026-07-12"}})

    scheduled_nightly._record_nightly_result("2026-07-12", failures=0)

    state = json.loads(memory_state.STATE_FILE.read_text(encoding="utf-8"))
    assert "nightly_catchup_claim" not in state
    assert state["last_nightly_status"] == "success"
    assert state["last_nightly_date"] == "2026-07-12"


def test_nightly_releases_claim_when_maintenance_lock_prevents_run(tmp_path, monkeypatch):
    import memory_state
    import scheduled_nightly

    state_root = tmp_path / "state"
    lock = state_root / "run" / "maintenance.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("999", encoding="utf-8")
    monkeypatch.setattr(memory_state, "STATE_DIR", state_root / "run")
    monkeypatch.setattr(memory_state, "STATE_FILE", state_root / "run" / "state.json")
    monkeypatch.setattr(memory_state, "LOCK_FILE", state_root / "run" / "state.json.lock")
    monkeypatch.setattr(scheduled_nightly, "STATE_ROOT", state_root)
    monkeypatch.setattr(scheduled_nightly, "update_state", memory_state.update_state)
    memory_state.save_state({
        "last_nightly_date": "2026-07-11",
        "last_nightly_status": "success",
        "nightly_catchup_claim": {
            "date": scheduled_nightly.datetime.now().strftime("%Y-%m-%d")
        }
    })

    assert scheduled_nightly.main() == 0

    state = json.loads(memory_state.STATE_FILE.read_text(encoding="utf-8"))
    assert "nightly_catchup_claim" not in state
    assert state["last_nightly_status"] == "success"
    assert state["last_nightly_date"] == "2026-07-11"
    assert state["last_nightly_skip"]["reason"] == "maintenance_lock_held"
    assert "last_nightly_failure" not in state


@pytest.mark.parametrize("role", ["nightly", "weekly"])
def test_maintenance_marker_remains_one_ascii_decimal_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    import markdown_transaction
    import operational_ownership

    state_root = tmp_path / "state"
    candidate = state_root / "run/markdown-transactions-v3.candidate.sqlite3"
    markdown_transaction.initialize_coordinator_v3_candidate(candidate, source_v2=None)
    marker_path = state_root / "run/maintenance.lock"
    real_acquire = operational_ownership.OwnershipRegistry.acquire
    parser = (
        "import os,pathlib,sys; raw=pathlib.Path(sys.argv[1]).read_bytes();"
        "assert raw.isascii() and raw.isdigit() and b'\\n' not in raw;"
        "assert int(raw)==int(sys.argv[2]); print('running')"
    )
    observed: list[str] = []

    def observe_acquire(registry, selected_role, **kwargs):
        result = subprocess.run(
            [sys.executable, "-c", parser, str(marker_path), str(os.getpid())],
            check=True,
            capture_output=True,
            text=True,
        )
        observed.append(result.stdout.strip())
        with sqlite3.connect(candidate) as database:
            assert database.execute(
                "SELECT COUNT(*) FROM maintenance_owners WHERE role=?", (role,)
            ).fetchone() == (0,)
        return real_acquire(registry, selected_role, **kwargs)

    monkeypatch.setattr(operational_ownership.OwnershipRegistry, "acquire", observe_acquire)
    lease, marker = operational_ownership.acquire_scheduled_owner(
        role, state_root=state_root
    )
    try:
        assert observed == ["running"]
        assert marker_path.read_bytes() == str(os.getpid()).encode("ascii")
    finally:
        operational_ownership.release_marker_owner(lease, marker)
    assert not marker_path.exists()


def test_nightly_source_compacts_telemetry_and_never_flushes_frontmatter():
    source = (Path(__file__).resolve().parent.parent / "scripts/scheduled_nightly.py").read_text(
        encoding="utf-8"
    )
    assert "from retrieval_telemetry import compact" in source
    assert "telemetry: compacted" in source
    assert "from access_tracking import flush_all" not in source


def test_the_nightly_pass_pays_the_backlinks_the_vault_owes():
    """The repair only helps if the pass that runs unattended actually calls it."""
    import scheduled_nightly

    steps = scheduled_nightly._post_compile_steps()
    labels = [step.label for step in steps]
    backlinks = next(step for step in steps if step.label == "backlinks")

    assert labels.index("backlinks") > labels.index("lint")
    assert backlinks.command[-1] == "--apply"
    assert backlinks.command[-2].endswith("repair_backlinks.py")


@pytest.mark.parametrize("status", ["deferred", "error"])
def test_generation_refresh_never_treats_deferred_or_error_as_success(monkeypatch, status):
    import scheduled_nightly

    monkeypatch.setattr(
        scheduled_nightly,
        "run_generation_maintenance",
        lambda **kwargs: {
            "status": status,
            "generation_id": "candidate",
            "partial": status == "deferred",
        },
    )
    messages = []

    assert scheduled_nightly._refresh_generation(messages.append) == 1
    assert any(status in message for message in messages)


def _state(monkeypatch, payload: dict) -> None:
    import scheduled_nightly

    monkeypatch.setattr(scheduled_nightly, "_safe_state", lambda: payload)


def test_a_compile_that_ran_and_failed_is_a_nightly_failure(monkeypatch):
    """The step spawns the compile and returns 0; the outcome lives in state."""
    import scheduled_nightly

    _state(
        monkeypatch,
        {
            "last_compile_finished_at": "2026-08-22T03:00:43",
            "last_compile_status": "error",
            "last_compile_error": "RuntimeError: no LLM provider",
        },
    )

    assert scheduled_nightly._compile_failed_this_pass("2026-08-21T03:00:41") == (
        "RuntimeError: no LLM provider"
    )


def test_an_older_failure_is_not_counted_against_this_pass(monkeypatch):
    """No compile ran tonight, so last night's error is not tonight's failure."""
    import scheduled_nightly

    stamp = "2026-08-21T03:00:41"
    _state(
        monkeypatch,
        {
            "last_compile_finished_at": stamp,
            "last_compile_status": "error",
            "last_compile_error": "RuntimeError: no LLM provider",
        },
    )

    assert scheduled_nightly._compile_failed_this_pass(stamp) is None


def test_a_compile_that_ran_and_committed_is_not_a_failure(monkeypatch):
    import scheduled_nightly

    _state(
        monkeypatch,
        {
            "last_compile_finished_at": "2026-08-22T03:00:43",
            "last_compile_status": "ok",
        },
    )

    assert scheduled_nightly._compile_failed_this_pass("2026-08-21T03:00:41") is None


def test_a_vault_that_never_compiled_reports_no_failure(monkeypatch):
    import scheduled_nightly

    _state(monkeypatch, {})

    assert scheduled_nightly._compile_failed_this_pass(None) is None
