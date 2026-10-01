"""Real locking diagnostics must not invalidate a live corpus ancestor seal."""

import os
import time
from pathlib import Path

import corpus_snapshot
import pytest
import reliable_memory

from tests.slow_machine import SHORT_TIMEOUT


def test_real_probe_preserves_the_sealed_vault_root(tmp_path):
    (tmp_path / "run").mkdir()
    seal = corpus_snapshot._identity(tmp_path, tmp_path.lstat())
    assert reliable_memory._sqlite_lock_probe(tmp_path) is True
    assert corpus_snapshot._identity(tmp_path, tmp_path.lstat()) == seal
    descriptor = corpus_snapshot._open_descriptor_chain((seal,), changed_error=PermissionError)
    os.close(descriptor)
    assert list((tmp_path / "run").iterdir()) == []


def test_read_only_probe_does_not_create_missing_runtime_directory(tmp_path):
    before = tmp_path.lstat()
    assert reliable_memory._sqlite_lock_probe(tmp_path) is None
    assert list(tmp_path.iterdir()) == []
    assert tmp_path.lstat().st_ctime_ns == before.st_ctime_ns


def test_probe_refuses_a_linked_run_directory(tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    root = tmp_path / "vault"
    root.mkdir()
    try:
        (root / "run").symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")
    assert reliable_memory._sqlite_lock_probe(root) is None
    assert list(external.iterdir()) == []


def test_initializer_creates_existing_contract_run_directory(tmp_path):
    root = tmp_path / "vault"
    reliable_memory.validate_state_root(root)
    assert (root / "run").is_dir()
    assert list((root / "run").iterdir()) == []
    assert not list(root.glob(".llm-wiki-lock-probe-*"))


def test_real_probe_keeps_two_connections_and_cleans_only_its_files(tmp_path, monkeypatch):
    runtime = tmp_path / "run"
    runtime.mkdir()
    retained = runtime / "retained.txt"
    retained.write_bytes(b"must remain")
    calls = []
    original = reliable_memory.sqlite3.connect

    def observe(database, *args, **kwargs):
        calls.append(Path(database))
        return original(database, *args, **kwargs)

    monkeypatch.setattr(reliable_memory.sqlite3, "connect", observe)
    assert reliable_memory._sqlite_lock_probe(tmp_path, deadline=time.monotonic() + SHORT_TIMEOUT) is True
    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert calls[0].parent == runtime
    assert retained.read_bytes() == b"must remain"
    assert list(runtime.iterdir()) == [retained]


def test_probe_refuses_a_different_runtime_filesystem(tmp_path, monkeypatch):
    (tmp_path / "run").mkdir()
    original = Path.lstat

    def changed_device(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == tmp_path / "run":
            fields = list(info)
            fields[2] += 1
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(Path, "lstat", changed_device)
    assert reliable_memory._sqlite_lock_probe(tmp_path) is None
    assert list((tmp_path / "run").iterdir()) == []


def test_replaced_probe_directory_is_refused_and_foreign_files_remain(tmp_path, monkeypatch):
    runtime = tmp_path / "run"
    runtime.mkdir()
    moved = tmp_path / "retained-run"
    foreign = []

    def replace_directory(probe, deadline, connections):
        runtime.rename(moved)
        runtime.mkdir()
        probe.write_bytes(b"foreign file")
        foreign.append(probe)
        return True

    monkeypatch.setattr(reliable_memory, "_run_lock_probe", replace_directory)
    assert reliable_memory._sqlite_lock_probe(tmp_path) is None
    assert foreign[0].read_bytes() == b"foreign file"


def test_revalidation_does_not_chmod_an_already_private_root(tmp_path, monkeypatch):
    if os.name != "posix":
        pytest.skip("POSIX owner permissions")
    tmp_path.chmod(0o700)
    (tmp_path / "run").mkdir(mode=0o700)
    original = Path.chmod
    calls = []

    def observe(path, mode, *args, **kwargs):
        calls.append(path)
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "chmod", observe)
    reliable_memory.validate_state_root(tmp_path)
    assert tmp_path not in calls


@pytest.mark.skipif(os.name == "nt", reason="system-owned ancestors are POSIX policy")
def test_probe_reuses_the_approved_system_owned_ancestor_policy(tmp_path, monkeypatch):
    import bounded_io

    real = tmp_path / "real"
    real.mkdir()
    root = real / "vault"
    (root / "run").mkdir(parents=True)
    link = tmp_path / "system-link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")
    monkeypatch.setattr(bounded_io, "_system_symlink", lambda candidate: candidate == link)
    assert reliable_memory._sqlite_lock_probe(link / "vault") is True
    assert list((root / "run").iterdir()) == []


def test_probe_refuses_an_ancestor_the_shared_policy_does_not_trust(tmp_path, monkeypatch):
    import bounded_io

    real = tmp_path / "real"
    real.mkdir()
    root = real / "vault"
    (root / "run").mkdir(parents=True)
    link = tmp_path / "user-link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")
    monkeypatch.setattr(bounded_io, "_system_symlink", lambda candidate: False)
    assert reliable_memory._sqlite_lock_probe(link / "vault") is None
    assert list((root / "run").iterdir()) == []
