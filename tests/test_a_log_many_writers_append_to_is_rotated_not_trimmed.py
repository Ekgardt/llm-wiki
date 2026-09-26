"""A log that independent hooks append to is rotated, never trimmed in place (audit 2026-09-26 C-12).

docs/research/2026-09-26-a-log-many-writers-append-to-is-rotated-not-trimmed.md
"""
from __future__ import annotations

import ast
from pathlib import Path

import doctor
import maintenance_helpers

from tests.test_hook_failures_reach_health import NOW, _line, _trail

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def test_a_long_hook_log_keeps_every_line_it_held(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(maintenance_helpers, "REPORTS_DIR", tmp_path / "logs")
    lines = [f"[2026-09-26T10:00:{second:02d}+00:00] hook: failure {second}" for second in range(60)]
    live = _trail(tmp_path, lines)
    before = live.read_bytes()

    moved = maintenance_helpers.trim_scheduler_logs(keep_bytes=64)

    previous = maintenance_helpers.rotated_log_previous(live)
    assert (moved, live.exists(), previous.read_bytes()) == (len(before), False, before)


def test_doctor_still_sees_a_failure_the_rotation_moved(tmp_path: Path) -> None:
    live = _trail(tmp_path, [_line(NOW)])
    live.replace(maintenance_helpers.rotated_log_previous(live))

    finding = doctor._hook_error_check(tmp_path, NOW)

    assert (finding["status"], finding["details"]["recent"]) == ("degraded", 1)


def _appends(function: ast.FunctionDef) -> bool:
    return any(
        isinstance(node, ast.Call)
        and any(isinstance(argument, ast.Constant) and argument.value in ("a", "ab") for argument in node.args)
        for node in ast.walk(function)
    )


def _log_names(function: ast.FunctionDef) -> set[str]:
    return {
        node.value
        for node in ast.walk(function)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.endswith(".log")
    }


def _appended_logs(path: Path) -> set[str]:
    functions = [node for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))) if isinstance(node, ast.FunctionDef)]
    return set().union(*(_log_names(function) for function in functions if _appends(function)))


def test_every_log_a_script_appends_to_is_rotated() -> None:
    """A log opened for append by our own code has writers the trim cannot fence."""
    appended = set().union(*(_appended_logs(path) for path in SCRIPTS.glob("*.py")))

    assert appended <= set(maintenance_helpers.ROTATED_LOG_NAMES)
    assert not appended & set(maintenance_helpers.SCHEDULER_LOG_NAMES)
