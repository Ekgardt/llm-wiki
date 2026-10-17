"""Help and invalid arguments must exit before a scheduled mutation starts."""

import ast
import importlib
import sys
from pathlib import Path

import pytest


def _execute_script_entry(module, arguments, monkeypatch):
    monkeypatch.setattr(sys, "argv", [module.__file__, *arguments])
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    entry = ast.Module(body=[tree.body[-1]], type_ignores=[])
    namespace = dict(vars(module), __name__="__main__")
    exec(compile(entry, module.__file__, "exec"), namespace)


@pytest.mark.parametrize("arguments, exit_code", [(["--help"], 0), (["-h"], 0), (["--unknown-option"], 2)])
@pytest.mark.parametrize("name, fence, skip", [
    ("scheduled_nightly", "take_scheduled_fence", "record_scheduled_skip"),
    ("scheduled_weekly", "_fence_after_waiting", "_skipped"),
])
def test_entry_arguments_exit_before_admission(name, fence, skip, arguments, exit_code, monkeypatch):
    module = importlib.import_module(name)
    calls = []
    monkeypatch.setattr(module, fence, lambda *a, **kw: calls.append("admitted"))
    monkeypatch.setattr(module, skip, lambda *a, **kw: 0)
    with pytest.raises(SystemExit) as error:
        _execute_script_entry(module, arguments, monkeypatch)
    assert error.value.code == exit_code
    assert calls == []


@pytest.mark.parametrize("name", ["scheduled_nightly", "scheduled_weekly"])
@pytest.mark.parametrize("argument, exit_code", [("--help", 0), ("--unknown-option", 2)])
def test_real_command_exits_without_creating_runtime(name, argument, exit_code, tmp_path):
    import os
    import subprocess

    from tests.slow_machine import SHORT_TIMEOUT

    repository = Path(__file__).resolve().parents[1]
    runtime = tmp_path / "uninitialized-runtime"
    environment = dict(os.environ, LLM_WIKI_ROOT=str(repository), LLM_WIKI_STATE_ROOT=str(runtime))
    result = subprocess.run(
        [sys.executable, str(repository / "scripts" / f"{name}.py"), argument],
        env=environment, capture_output=True, text=True, timeout=SHORT_TIMEOUT,
    )
    assert result.returncode == exit_code, result.stderr
    assert "usage:" in result.stdout + result.stderr
    assert not runtime.exists()
