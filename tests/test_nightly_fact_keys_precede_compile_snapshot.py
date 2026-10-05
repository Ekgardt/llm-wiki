"""The night's own entity writer must finish before compile freezes its targets."""
import hashlib

import compile_memory as compiler
import pytest
import scheduled_nightly as nightly


def _manifest(path, logical):
    return {"entries": [{"path": logical, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}]}


def _step_runner(path, logical, snapshots, seen):
    def step(command, log, name, *, timeout):
        seen.append(name)
        if name == "fact_keys":
            path.write_text("committed entity facts\n")
        if name == "maybe_compile":
            snapshots.append(compiler.CompileInputs((), (), (
                compiler.TargetSnapshot(logical, path.read_bytes(), hashlib.sha256(path.read_bytes()).hexdigest()),
            )))
        return 0

    return step


def _finish_runner(path, logical, snapshots, external_edit):
    def finish():
        if external_edit:
            path.write_text("independent external change\n")
        compiler._require_current_compile_targets(snapshots[0], _manifest(path, logical))
        return True

    return finish


def _night(tmp_path, monkeypatch, external_edit):
    logical = "knowledge/notes/ledger-test.md"
    path = tmp_path / logical
    path.parent.mkdir(parents=True)
    path.write_text("before entity facts\n")
    snapshots = []
    seen = []

    step = _step_runner(path, logical, snapshots, seen)
    finish = _finish_runner(path, logical, snapshots, external_edit)

    monkeypatch.setattr(nightly, "_wait_for_compile_idle", lambda log: None)
    monkeypatch.setattr(nightly, "_last_compile_finished", lambda: None)
    monkeypatch.setattr(nightly, "_last_compile_started", lambda: None)
    monkeypatch.setattr(nightly, "_wait_compile_finished", finish)
    monkeypatch.setattr(nightly, "_report_compile_outcome", lambda *args: 0)
    monkeypatch.setattr(nightly, "_report_deferred_loss", lambda log: 0)
    monkeypatch.setattr(nightly, "_post_compile_pass", lambda *args: 0)
    result = nightly._nightly_steps(step, nightly.StepLog(lambda message: None))
    return result, seen, snapshots[0]


def test_own_entity_update_is_in_the_compile_target_snapshot(tmp_path, monkeypatch):
    result, seen, snapshot = _night(tmp_path, monkeypatch, False)
    assert result == 0
    assert snapshot.targets[0].content == b"committed entity facts\n"
    assert seen.index("fact_keys") < seen.index("maybe_compile")


def test_external_target_edit_still_requires_a_fresh_model_plan(tmp_path, monkeypatch):
    with pytest.raises(compiler.TransactionFailure, match="compile target snapshot changed"):
        _night(tmp_path, monkeypatch, True)
