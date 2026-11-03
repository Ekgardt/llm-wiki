"""Probe scratch is owned only while its directory identity is unchanged."""
from llm_wiki import sqlite_io


def test_root_probe_initializes_only_run_and_removes_scratch(tmp_path):
    sqlite_io.validate_state_root(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ['run']
    assert list((tmp_path / 'run').iterdir()) == []
    assert sqlite_io._sqlite_lock_probe(tmp_path, deadline=0) is None
    assert list((tmp_path / 'run').iterdir()) == []


def test_replaced_directory_never_allows_foreign_cleanup(tmp_path, monkeypatch):
    runtime = tmp_path / 'run'
    runtime.mkdir()
    foreign = []

    def replace(probe, deadline, connections):
        runtime.rename(tmp_path / 'old-run')
        runtime.mkdir()
        probe.write_bytes(b'foreign')
        foreign.append(probe)
        return True

    monkeypatch.setattr(sqlite_io, '_run_lock_probe', replace)
    assert sqlite_io._sqlite_lock_probe(tmp_path) is None
    assert foreign[0].read_bytes() == b'foreign'
