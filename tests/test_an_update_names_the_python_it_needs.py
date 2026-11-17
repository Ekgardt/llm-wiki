"""An update never lands on code this machine's Python cannot run, and a nightly recovers.

docs/research/2026-11-09-an-update-names-the-python-it-needs.md
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
import python_series
import scheduled_nightly
import self_update
from llm_wiki import doctor_schedule

from tests.self_update_helpers import _commit, _git, _repository
from tests.slow_machine import LONG_TIMEOUT

REPOSITORY = Path(__file__).resolve().parent.parent


def _head(root: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True).stdout


@pytest.fixture()
def clone(tmp_path: Path) -> SimpleNamespace:
    upstream = _repository(tmp_path / "upstream")
    _commit(upstream, "product.py", "value = 1\n")
    checkout = tmp_path / "clone"
    subprocess.run(["git", "clone", "--quiet", str(upstream), str(checkout)], check=True, capture_output=True)
    _git(checkout, "config", "user.email", "test@example.com")
    _git(checkout, "config", "user.name", "Test")
    return SimpleNamespace(upstream=upstream, checkout=checkout)


def test_a_commit_needing_a_python_this_machine_lacks_is_not_taken(clone) -> None:
    _commit(clone.upstream, "pyproject.toml", '[project]\nname = "x"\nrequires-python = ">=3.99"\n')
    before = _head(clone.checkout)

    outcome = self_update.update_checkout(clone.checkout)

    assert (outcome["status"], outcome["reason"], outcome["required"]) == ("skipped", "python_requirement_unmet", ">=3.99")
    assert _head(clone.checkout) == before


def test_a_commit_this_machine_can_run_is_taken(clone, monkeypatch) -> None:
    _commit(clone.upstream, "pyproject.toml", '[project]\nname = "x"\nrequires-python = ">=3.0"\n')
    monkeypatch.setattr(self_update, "synced_dependencies", lambda _root, _extras: True)

    assert self_update.update_checkout(clone.checkout)["status"] == "updated"


def test_an_unreadable_next_pyproject_stops_the_update(clone) -> None:
    _commit(clone.upstream, "pyproject.toml", "[project\n")
    before = _head(clone.checkout)

    assert self_update.update_checkout(clone.checkout)["status"] != "updated"
    assert _head(clone.checkout) == before


def test_the_operator_is_told_which_python_to_install(clone) -> None:
    _commit(clone.upstream, "pyproject.toml", '[project]\nname = "x"\nrequires-python = ">=3.99"\n')

    record = scheduled_nightly.update_record(self_update.update_checkout(clone.checkout))
    status, message = doctor_schedule.update_verdict(record)

    assert status == "degraded"
    assert "uv python install '>=3.99'" in message


def test_the_supported_series_is_the_one_the_project_requires() -> None:
    project = tomllib.loads((REPOSITORY / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    major, minor = python_series.SUPPORTED_SERIES

    assert project["requires-python"] == f">={major}.{minor},<{major}.{minor + 1}"


@pytest.mark.parametrize("script", ["python_series.py", "scheduled_nightly.py"])
def test_the_oldest_python_a_vault_may_still_have_can_parse_what_runs_first(script: str) -> None:
    ast.parse((REPOSITORY / "scripts" / script).read_text(encoding="utf-8"), feature_version=(3, 10))


def test_the_gate_imports_only_what_every_python_ships() -> None:
    tree = ast.parse((REPOSITORY / "scripts" / "python_series.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            imported.update({node.module} if isinstance(node, ast.ImportFrom) else {alias.name for alias in node.names})

    assert imported <= {"__future__", "pathlib", "site", "subprocess", "sys"}


def _needs_the_current_python(node: ast.stmt) -> bool:
    """`datetime.UTC` is absent from the oldest Pythons; project modules may use anything."""
    if not isinstance(node, ast.ImportFrom):
        return False
    return node.module == "datetime" or node.module.split(".")[0] not in sys.stdlib_module_names


def _top_level_lines(tree: ast.Module, wanted) -> list[int]:
    return [node.lineno for node in tree.body if wanted(node)]


def test_the_nightly_passes_the_gate_before_any_newer_import() -> None:
    tree = ast.parse((REPOSITORY / "scripts" / "scheduled_nightly.py").read_text(encoding="utf-8"))
    gate = _top_level_lines(tree, lambda node: isinstance(node, ast.Expr) and "require_supported_python" in ast.dump(node))
    sensitive_imports = _top_level_lines(tree, _needs_the_current_python)

    assert len(gate) == 1 and gate[0] < min(sensitive_imports)


def _child(stdout: str = "", returncode: int = 0, stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def _older_python(monkeypatch, children: list[SimpleNamespace]) -> list[SimpleNamespace]:
    calls: list[SimpleNamespace] = []

    def run(command, **options):
        calls.append(SimpleNamespace(command=list(command), timeout=options["timeout"]))
        return children.pop(0)

    monkeypatch.setattr(python_series.sys, "version_info", (3, 12, 0, "final", 0))
    monkeypatch.setattr(python_series.subprocess, "run", run)
    monkeypatch.setattr(python_series.site, "getsitepackages", lambda: ["/old/site-packages"])
    return calls


FOUND = _child(stdout="/managed/python3.14\n")


def test_the_supported_python_passes_without_a_probe(monkeypatch) -> None:
    calls = _older_python(monkeypatch, [])
    monkeypatch.setattr(python_series.sys, "version_info", (*python_series.SUPPORTED_SERIES, 0, "final", 0))

    python_series.require_supported_python(Path("."))

    assert calls == []


def test_the_new_python_moves_the_environment_then_this_nightly_reruns_and_keeps_its_exit(monkeypatch) -> None:
    calls = _older_python(monkeypatch, [FOUND, _child(), _child(stdout="1800.0\n"), _child(returncode=7)])
    monkeypatch.setattr(python_series.sys, "argv", ["scheduled_nightly.py", "--flag"])

    with pytest.raises(SystemExit) as stopped:
        python_series.require_supported_python(Path("/vault"))

    assert stopped.value.code == 7
    assert calls[1].command[0] == "/managed/python3.14"
    assert calls[1].command[2:] == ["--repair-environment", str(Path("/vault")), "--site=/old/site-packages"]
    assert calls[3].command[-2:] == ["scheduled_nightly.py", "--flag"]


def test_a_machine_without_the_python_gets_it_from_uv(monkeypatch) -> None:
    calls = _older_python(
        monkeypatch, [_child(returncode=2), _child(), FOUND, _child(), _child(stdout="1.0"), _child()]
    )

    with pytest.raises(SystemExit):
        python_series.require_supported_python(Path("."))

    assert calls[1].command == ["uv", "python", "install", "3.14"]
    assert calls[3].command[0] == "/managed/python3.14"


def test_the_rerun_is_bounded_by_what_the_nightly_says_it_can_take(monkeypatch) -> None:
    calls = _older_python(monkeypatch, [FOUND, _child(), _child(stdout="1800.0\n"), _child()])

    with pytest.raises(SystemExit):
        python_series.require_supported_python(Path("."))

    assert calls[3].timeout == 1800.0 + python_series.PROBE_TIMEOUT_SECONDS


def test_the_real_probe_reads_the_nightly_bound(tmp_path) -> None:
    done = subprocess.run(
        [sys.executable, "-c", python_series.NIGHTLY_BOUND_PROBE, str(REPOSITORY / "scripts")],
        capture_output=True, text=True, timeout=python_series.PROBE_TIMEOUT_SECONDS, check=False,
        env={**os.environ, "LLM_WIKI_ROOT": str(REPOSITORY), "LLM_WIKI_STATE_ROOT": str(tmp_path)},
    )

    assert done.returncode == 0, done.stderr
    assert float(done.stdout) > 0


@pytest.mark.parametrize(
    ("children", "said"),
    [
        ([_child(returncode=2), _child(returncode=9, stderr="token=hunter2")], "uv exit 9"),
        ([_child(returncode=2), _child(), _child(returncode=2)], "cannot be found"),
        ([FOUND, _child(returncode=1, stderr="noise\nno space left\n")], "no space left"),
        ([FOUND, _child(returncode=4)], "exit 4"),
        ([FOUND, _child(), _child(stdout="")], "could not start"),
    ],
    ids=["download refused", "download vanished", "sync says why", "sync silent", "nightly will not start"],
)
def test_each_way_the_recovery_stops_says_its_own_reason(monkeypatch, children, said) -> None:
    _older_python(monkeypatch, children)

    with pytest.raises(SystemExit) as stopped:
        python_series.require_supported_python(Path("."))

    assert said in str(stopped.value.code)
    assert "hunter2" not in str(stopped.value.code)


_FAKE_UV = """#!/bin/sh
echo "$*" >> "$FAKE_UV_LOG"
case "$1 $2" in
  "python find") echo "$FAKE_INTERPRETER" ;;
  "run --project")
    case " $* " in
      *" -c "*) echo 1800.0 ;;
      *) exit 7 ;;
    esac ;;
esac
"""
_OLD_PYTHON_GATE = """
import datetime, site, subprocess, sys
from pathlib import Path
sys.modules["tomllib"] = None
del datetime.UTC
sys.version_info = (3, 10, 0, "final", 0)
sys.argv = ["scheduled_nightly.py"]
sys.path.insert(0, {scripts!r})
import python_series
python_series.require_supported_python(Path({root!r}))
"""


@pytest.mark.skipif(sys.platform == "win32", reason="a shell stub stands in for uv")
def test_the_whole_recovery_runs_on_an_interpreter_missing_what_the_oldest_lacks(tmp_path) -> None:
    uv = tmp_path / "bin" / "uv"
    uv.parent.mkdir()
    uv.write_text(_FAKE_UV, encoding="utf-8")
    uv.chmod(0o755)
    log = tmp_path / "uv.log"
    environment = {
        **os.environ,
        "PATH": f"{uv.parent}{os.pathsep}{os.environ['PATH']}",
        "FAKE_UV_LOG": str(log),
        "FAKE_INTERPRETER": sys.executable,
    }
    script = _OLD_PYTHON_GATE.format(scripts=str(REPOSITORY / "scripts"), root=str(REPOSITORY))

    done = subprocess.run(
        [sys.executable, "-I", "-c", script],
        capture_output=True, text=True, env=environment, timeout=LONG_TIMEOUT, check=False,
    )

    assert done.returncode == 7, done.stderr
    said = log.read_text(encoding="utf-8")
    assert "sync --locked --no-default-groups" in said
    assert "scheduled_nightly.py" in said


def _old_environment_with_pytest(tmp_path: Path) -> Path:
    site_packages = tmp_path / "old-site"
    metadata = site_packages / "pytest-9.0.0.dist-info" / "METADATA"
    metadata.parent.mkdir(parents=True)
    metadata.write_text("Metadata-Version: 2.1\nName: pytest\nVersion: 9.0.0\n", encoding="utf-8")
    return site_packages


_SECRET = "ghp_" + "a" * 36


@pytest.fixture()
def sync(monkeypatch) -> SimpleNamespace:
    observed = SimpleNamespace(commands=[], exit_code=0)

    def run(command, **_options):
        observed.commands.append(tuple(command))
        return subprocess.CompletedProcess(command, observed.exit_code, "", f"uv: {_SECRET} failed")

    monkeypatch.setattr(self_update, "run", run)
    return observed


def test_the_repair_keeps_what_the_old_environment_had_chosen(tmp_path, sync) -> None:
    site_packages = _old_environment_with_pytest(tmp_path)

    code = self_update.main(["--repair-environment", str(REPOSITORY), "--site", str(site_packages)])

    assert code == 0
    assert "--group" in sync.commands[0]


def test_a_failed_repair_exits_nonzero_and_redacts_the_reason(tmp_path, sync, capsys) -> None:
    sync.exit_code = 1

    code = self_update.main(["--repair-environment", str(REPOSITORY), "--site", str(tmp_path)])

    assert code == 1
    said = capsys.readouterr().err
    assert "uv:" in said and _SECRET not in said


@pytest.mark.skipif(sys.platform == "win32", reason="a shell stub stands in for uv")
def test_the_repair_needs_nothing_from_the_environment_it_replaces(tmp_path) -> None:
    uv = tmp_path / "bin" / "uv"
    uv.parent.mkdir()
    uv.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    uv.chmod(0o755)
    script = str(REPOSITORY / "scripts" / "self_update.py")

    done = subprocess.run(
        [sys.executable, "-S", script, "--repair-environment", str(REPOSITORY), "--site", str(tmp_path)],
        capture_output=True, text=True, env={**os.environ, "PATH": f"{uv.parent}{os.pathsep}{os.environ['PATH']}"},
        timeout=LONG_TIMEOUT, check=False,
    )

    assert done.returncode == 0, done.stderr
