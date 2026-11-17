"""Helpers the managed Pyright installer tests share with each other and with its guard tests."""
from __future__ import annotations

import time
from pathlib import Path

import install_pyright as installer_module
import pytest
from install_pyright import PyrightInstallError, install_pyright


def _installer_entries(state_root: Path) -> tuple[str, ...]:
    parent = state_root / "cache/code-tools/pyright"
    if not parent.exists():
        return ()
    return tuple(sorted(path.name for path in parent.iterdir()))


def _assert_no_owned_scratch(state_root: Path) -> None:
    assert all(not name.startswith(".install-pyright-") for name in _installer_entries(state_root))


def _error_code(error: pytest.ExceptionInfo[PyrightInstallError]) -> str:
    return error.value.code


def assert_state_validation_honors_end_to_end_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_deadlines: list[float] = []
    legacy_calls: list[Path] = []
    now = [time.monotonic()]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])

    def slow_legacy_validation(path: Path) -> None:
        legacy_calls.append(path)
        now[0] += 0.25

    def bounded_lock_probe(path: Path, *, deadline: float):
        observed_deadlines.append(deadline)
        now[0] = deadline
        return None

    monkeypatch.setattr(
        installer_module,
        "validate_state_root",
        slow_legacy_validation,
        raising=False,
    )
    monkeypatch.setattr(
        installer_module,
        "sqlite_lock_probe",
        bounded_lock_probe,
        raising=False,
    )
    started = time.monotonic()
    requested_deadline = started + 0.02

    with pytest.raises(TimeoutError, match="deadline"):
        install_pyright(
            state_root=tmp_path / "state",
            deadline=requested_deadline,
        )

    assert legacy_calls == []
    assert observed_deadlines == [requested_deadline]
    assert time.monotonic() == requested_deadline
    assert not (tmp_path / "state/cache").exists()
