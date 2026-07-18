from __future__ import annotations

import json
from pathlib import Path


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


def test_nightly_source_compacts_telemetry_and_never_flushes_frontmatter():
    source = (Path(__file__).resolve().parent.parent / "scripts/scheduled_nightly.py").read_text(
        encoding="utf-8"
    )
    assert "from retrieval_telemetry import compact" in source
    assert "telemetry: compacted" in source
    assert "from access_tracking import flush_all" not in source
