"""Validate immutable artifacts separately from a changing or unsafe live corpus."""
import hashlib
import os
import time
from datetime import datetime, timezone

import pytest

from tests.slow_machine import LONG_TIMEOUT
from tests.test_generation_maintenance import _generation_directory, _vault


def _built(tmp_path):
    import doctor

    root, state = _vault(tmp_path)
    (root / "knowledge/notes/page.md").write_bytes(b"---\ntype: concept\n---\n# Page\n")
    built = doctor.run_generation_maintenance(
        root=root, state_root=state, time_budget_seconds=LONG_TIMEOUT, max_sources=10
    )
    return root, state, built, _generation_directory(state, built)


@pytest.mark.skipif(os.name != "posix", reason="real POSIX descriptor race")
def test_live_ancestor_change_does_not_claim_artifact_corruption(tmp_path, monkeypatch):
    import corpus_snapshot
    import doctor

    root, state, built, directory = _built(tmp_path)
    evidence = directory / "evidence.sqlite3"
    before = hashlib.sha256(evidence.read_bytes()).hexdigest()
    original = corpus_snapshot._open_sealed_root
    changed = []

    def race(expected, flags, changed_error):
        if not changed:
            (root / "concurrent-source-marker").write_bytes(b"independent editor")
            changed.append(True)
        return original(expected, flags, changed_error)

    monkeypatch.setattr(corpus_snapshot, "_open_sealed_root", race)
    result = doctor._generation_check(
        root, state, datetime.now(timezone.utc), deadline=time.monotonic() + LONG_TIMEOUT
    )
    assert changed
    assert result["status"] == "error"
    assert result["message"].startswith("Live corpus could not be verified")
    details = result["details"]
    assert details["catalog"] == "valid"
    assert details["active_generation"] == built["generation_id"]
    assert details["live_corpus_state"] == "unverified"
    assert details["validation_error"].startswith("PermissionError:")
    assert details["freshness"] == "unknown"
    assert details["partial"] is True
    assert details["repairable"] is False
    assert hashlib.sha256(evidence.read_bytes()).hexdigest() == before


def test_invalid_live_settings_still_refuse_without_mislabeling_artifacts(tmp_path, monkeypatch):
    import corpus_snapshot
    import doctor

    root, state, _built_result, _directory = _built(tmp_path)
    original = corpus_snapshot.collect_corpus

    def unsupported_source(*args, **kwargs):
        (root / "llm-wiki.toml").write_bytes(b"[corpus]\nmax_files = 0\n")
        return original(*args, **kwargs)

    monkeypatch.setattr(corpus_snapshot, "collect_corpus", unsupported_source)
    result = doctor._generation_check(
        root, state, datetime.now(timezone.utc), deadline=time.monotonic() + LONG_TIMEOUT
    )
    assert result["status"] == "error"
    assert result["details"]["live_corpus_state"] == "unverified"
    assert result["details"]["validation_error"].startswith("SettingsError:")
    assert result["details"]["repairable"] is False


def test_damaged_immutable_manifest_keeps_corruption_handling(tmp_path):
    import doctor

    root, state, _built_result, directory = _built(tmp_path)
    (directory / "manifest.json").write_bytes(b"not a valid manifest")
    result = doctor._generation_check(
        root, state, datetime.now(timezone.utc), deadline=time.monotonic() + LONG_TIMEOUT
    )
    assert result["status"] == "error"
    assert "artifacts are invalid" in result["message"]
    assert result["details"].get("live_corpus_state") != "unverified"
