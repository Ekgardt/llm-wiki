"""The guard rejects the actual obsolete call, not a slow filesystem's clock."""
from pathlib import Path

import pytest

from tests import test_install_pyright as installer_tests


def test_the_deadline_guard_rejects_the_unbounded_legacy_validator(tmp_path, monkeypatch):
    module = installer_tests.installer_module

    def unbounded(path, deadline):
        module.validate_state_root(path)

    monkeypatch.setattr(module, "_validate_installer_state_root", unbounded)
    with pytest.raises(AssertionError):
        installer_tests.test_state_validation_honors_end_to_end_deadline(tmp_path, monkeypatch)


def test_a_filesystem_returning_after_deadline_cannot_start_the_probe(tmp_path, monkeypatch):
    module = installer_tests.installer_module
    clock = [module.time.monotonic()]
    state = tmp_path / "state"
    original = Path.lstat
    delayed = []
    probes = []

    def late_stat(path, *args, **kwargs):
        try:
            return original(path, *args, **kwargs)
        finally:
            if path == state and not delayed:
                delayed.append(path)
                clock[0] += 0.25

    def probe(path, *, deadline):
        probes.append(path)
        return True

    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(Path, "lstat", late_stat)
    monkeypatch.setattr(module, "_sqlite_lock_probe", probe)
    with pytest.raises(TimeoutError, match="deadline"):
        module.install_pyright(state_root=state, deadline=clock[0] + 0.02)
    assert delayed == [state]
    assert probes == []
    assert not state.exists()
