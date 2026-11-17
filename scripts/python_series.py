"""Bring the nightly back when the checkout has moved past the environment's Python.

A release that did not check the next commit's Python can fast-forward onto code
its environment cannot import, and every later nightly would then die at import,
before the retry of the sync. So this runs first and imports only the standard
library of the oldest Python a vault may still have: the work that needs the new
Python is handed to the new Python. See
`docs/research/2026-11-09-an-update-names-the-python-it-needs.md`.
"""
from __future__ import annotations

import site
import subprocess
import sys
from pathlib import Path

SUPPORTED_SERIES = (3, 14)
NIGHTLY_BOUND_PROBE = (
    f"import sys; tuple(sys.version_info[:2]) == {SUPPORTED_SERIES} or sys.exit(1); "
    "sys.path.insert(0, sys.argv[1]); import nightly_budget; print(nightly_budget.worst_case_seconds())"
)
# Basis: the 60 s `self_update` allows one local git or uv query. This one starts an
# already synced environment and imports one module.
PROBE_TIMEOUT_SECONDS = 60.0
# Basis: `self_update.SYNC_TIMEOUT_SECONDS`, what one uv download-and-install step gets.
_INSTALL_TIMEOUT_SECONDS = 600.0
_NO_INTERPRETER = (
    "This checkout needs Python {series}, and uv could not provide it ({why}). "
    "Run: uv python install {series}. The next nightly then repairs the environment."
)
_NOT_MOVED = (
    "This checkout needs Python {series}, which is present, but the environment could not be "
    "moved to it: {why}. The next nightly tries again; rerunning the installer does it now."
)
_NOT_STARTING = (
    "The environment was moved to Python {series}, but the nightly could not start on it. "
    "Rerun the installer."
)


def _series_text() -> str:
    return ".".join(str(part) for part in SUPPORTED_SERIES)


def _on_supported_series() -> bool:
    return tuple(sys.version_info[:2]) == SUPPORTED_SERIES


def _environment_command(root: Path, *arguments: str) -> list[str]:
    return ["uv", "run", "--project", str(root), "--locked", "--no-sync", "python", *arguments]


def _bounded_call(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    """One child, whose failure to start or finish reads like any other failure."""
    try:
        return subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        return subprocess.CompletedProcess(command, -1, "", type(error).__name__)


def _found_interpreter() -> str:
    found = _bounded_call(["uv", "python", "find", _series_text()], PROBE_TIMEOUT_SECONDS)
    return found.stdout.strip() if found.returncode == 0 else ""


def _refuse_no_interpreter(why: str) -> SystemExit:
    return SystemExit(_NO_INTERPRETER.format(series=_series_text(), why=why))


def _supported_interpreter() -> str:
    """Python of the supported series, fetched by uv when this machine has none.

    Nothing can run here until it exists and nobody is present at 03:00, so unlike
    the installer this step downloads rather than waits to be told.
    """
    return _found_interpreter() or _downloaded_interpreter()


def _downloaded_interpreter() -> str:
    installed = _bounded_call(["uv", "python", "install", _series_text()], _INSTALL_TIMEOUT_SECONDS)
    if installed.returncode != 0:
        raise _refuse_no_interpreter(installed.stderr if installed.returncode == -1 else f"uv exit {installed.returncode}")
    interpreter = _found_interpreter()
    if not interpreter:
        raise _refuse_no_interpreter("it was installed but cannot be found")
    return interpreter


def _environment_move_failure(root: Path, interpreter: str) -> str | None:
    """Have the supported interpreter sync the environment; None, or its last words."""
    sites = [f"--site={path}" for path in site.getsitepackages()]
    script = str(root / "scripts" / "self_update.py")
    command = [interpreter, script, "--repair-environment", str(root), *sites]
    moved = _bounded_call(command, _INSTALL_TIMEOUT_SECONDS + PROBE_TIMEOUT_SECONDS)
    if moved.returncode == 0:
        return None
    last_line = moved.stderr.strip().splitlines()[-1:]
    return last_line[0] if last_line else f"exit {moved.returncode}"


def _nightly_worst_case_seconds(root: Path) -> float | None:
    """The nightly's own bound, read on the repaired Python; None if it cannot run there."""
    command = _environment_command(root, "-c", NIGHTLY_BOUND_PROBE, str(root / "scripts"))
    try:
        return float(_bounded_call(command, PROBE_TIMEOUT_SECONDS).stdout)
    except ValueError:
        return None


def _rerun_nightly(root: Path, worst_case_seconds: float) -> None:
    startup_allowance = PROBE_TIMEOUT_SECONDS
    rerun = subprocess.run(
        _environment_command(root, *sys.argv), check=False, timeout=worst_case_seconds + startup_allowance
    )
    raise SystemExit(rerun.returncode)


def _repair_environment(root: Path) -> None:
    failure = _environment_move_failure(root, _supported_interpreter())
    if failure is not None:
        raise SystemExit(_NOT_MOVED.format(series=_series_text(), why=failure))


def _repaired_nightly_bound(root: Path) -> float:
    worst_case_seconds = _nightly_worst_case_seconds(root)
    if worst_case_seconds is None:
        raise SystemExit(_NOT_STARTING.format(series=_series_text()))
    return worst_case_seconds


def require_supported_python(root: Path) -> None:
    """Return on the supported Python; otherwise repair and rerun, or stop and say why."""
    if _on_supported_series():
        return
    _repair_environment(root)
    _rerun_nightly(root, _repaired_nightly_bound(root))
