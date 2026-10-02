"""A generation snapshot participates in the canonical Markdown writer gate."""
import time
from pathlib import Path
from types import SimpleNamespace

import corpus_snapshot
import doctor
import pytest
from markdown_transaction import active_or_legacy_coordinator

from tests.slow_machine import LONG_TIMEOUT
from tests.test_reliability_v3_adoption import _vault, build_adopted_reliability_v3


def test_snapshot_discovery_holds_the_supplied_canonical_writer_gate(tmp_path: Path, monkeypatch):
    root, state = _vault(tmp_path)
    build_adopted_reliability_v3(root, state)
    coordinator = active_or_legacy_coordinator(root, state)
    discovery = corpus_snapshot._discover
    observed = []

    def checked_discovery(*args, **kwargs):
        observed.append(coordinator.writer_gate_held())
        assert coordinator.writer_gate_held(), "snapshot discovery is outside its writer fence"
        return discovery(*args, **kwargs)

    monkeypatch.setattr(corpus_snapshot, "_discover", checked_discovery)
    result = doctor._build_or_refresh_generation(
        root, state, deadline=time.monotonic() + LONG_TIMEOUT,
        cancelled=lambda: False, max_sources=doctor.setting_value("corpus.max_files", root),
        force_rebuild=True, coordinator=coordinator,
    )
    assert result["status"] == "built"
    assert observed
    assert not coordinator.writer_gate_held(), "snapshot capture must release its writer fence"


def test_doctor_repair_passes_its_actual_coordinator_to_generation_build(tmp_path: Path, monkeypatch):
    root, state = _vault(tmp_path)
    build_adopted_reliability_v3(root, state)
    coordinator = active_or_legacy_coordinator(root, state)
    guard = SimpleNamespace(coordinator=coordinator, cancelled=lambda: False)
    guard.run = lambda function, *args, **kwargs: function(*args, **kwargs)
    context = SimpleNamespace(root_path=root, state_path=state, deadline=time.monotonic()+LONG_TIMEOUT, rebuild_generation=True, repaired=[])
    calls = []

    def checked_build(*args, **kwargs):
        calls.append(kwargs.get("coordinator"))
        assert kwargs.get("coordinator") is coordinator, "doctor dropped its canonical coordinator"
        return {"status": "built", "generation_id": "fixture-generation"}

    monkeypatch.setattr(doctor, "_repair_generation_catalog", lambda *args, **kwargs: None)
    monkeypatch.setattr(doctor, "_build_or_refresh_generation", checked_build)
    doctor._repair_generations_action(guard, context)
    assert calls == [coordinator]
    assert context.repaired == [{"action": "rebuild_generation", "generation_id": "fixture-generation"}]


@pytest.mark.parametrize("stop", ["discovery_failure", "cancelled"])
def test_failed_or_cancelled_snapshot_releases_the_writer_gate(tmp_path: Path, monkeypatch, stop: str):
    root, state = _vault(tmp_path)
    build_adopted_reliability_v3(root, state)
    coordinator = active_or_legacy_coordinator(root, state)
    observed = []

    def failing_discovery(*args, **kwargs):
        observed.append(coordinator.writer_gate_held())
        raise RuntimeError("injected source discovery failure")

    monkeypatch.setattr(corpus_snapshot, "_discover", failing_discovery)
    expected = {"discovery_failure": RuntimeError, "cancelled": TimeoutError}[stop]
    with pytest.raises(expected):
        doctor._build_or_refresh_generation(
            root, state, deadline=time.monotonic() + LONG_TIMEOUT,
            cancelled=lambda: stop == "cancelled",
            max_sources=doctor.setting_value("corpus.max_files", root),
            force_rebuild=True, coordinator=coordinator,
        )
    assert observed == {"discovery_failure": [True], "cancelled": []}[stop]
    assert not coordinator.writer_gate_held()
    with coordinator.writer_gate():
        assert coordinator.writer_gate_held(), "a failed capture must not leave an owner blocking the next writer"
