"""The night's own entity writer must finish before compile freezes its targets."""
import hashlib

import markdown_transaction
import pytest
import scheduled_nightly as nightly
from llm_wiki import compile_pipeline_receipts, compile_pipeline_sources


def _manifest(path, logical):
    return {"entries": [{"path": logical, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}]}


def _step_runner(path, logical, snapshots, seen, external_edit):
    def step(command, log, name, *, timeout):
        seen.append(name)
        if name == "fact_keys":
            path.write_bytes(b"committed entity facts\n")
        if name == "compile":
            snapshots.append(compile_pipeline_sources.CompileInputs((), (), (
                compile_pipeline_sources.TargetSnapshot(logical, path.read_bytes(), hashlib.sha256(path.read_bytes()).hexdigest()),
            )))
            _finish_runner(path, logical, snapshots, external_edit)()
        return 0

    return step


def _finish_runner(path, logical, snapshots, external_edit):
    def finish():
        if external_edit:
            path.write_bytes(b"independent external change\n")
        compile_pipeline_receipts.require_current_compile_targets(snapshots[0], _manifest(path, logical))
        return True

    return finish


def _night(tmp_path, monkeypatch, external_edit):
    logical = "knowledge/notes/ledger-test.md"
    path = tmp_path / logical
    path.parent.mkdir(parents=True)
    path.write_bytes(b"before entity facts\n")
    snapshots = []
    seen = []

    step = _step_runner(path, logical, snapshots, seen, external_edit)

    monkeypatch.setattr(nightly, "_wait_for_compile_idle", lambda log: None)
    monkeypatch.setattr(nightly, "compile_running", lambda: False)
    monkeypatch.setattr(nightly, "post_compile_pass", lambda *args: 0)
    result = nightly.nightly_steps(step, nightly.StepLog(lambda message: None))
    return result, seen, snapshots[0]


def test_own_entity_update_is_in_the_compile_target_snapshot(tmp_path, monkeypatch):
    result, seen, snapshot = _night(tmp_path, monkeypatch, False)
    assert result == 0
    assert snapshot.targets[0].content == b"committed entity facts\n"
    assert seen.index("fact_keys") < seen.index("compile")


def test_external_target_edit_still_requires_a_fresh_model_plan(tmp_path, monkeypatch):
    with pytest.raises(markdown_transaction.TransactionFailure, match="compile target snapshot changed"):
        _night(tmp_path, monkeypatch, True)
