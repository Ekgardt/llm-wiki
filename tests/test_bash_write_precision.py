"""The precision patch decides correctly, checked before it is applied as root.

`docs/enforcement/apply-bash-write-precision.py` rewrites a live gate. Reading
its output and agreeing it looks right is not evidence. This builds the patched
sources into a temporary copy of the enforcement tree and asks the real module
what it decides, in a subprocess so the installed modules stay unimported.

Skips where the gate is not installed.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "docs" / "enforcement" / "apply-bash-write-precision.py"
ENFORCEMENT = Path("/etc/claude-code/enforcement")
# The gate's own interpreter. `shell_ast` needs bashlex, which is installed
# there and not in this repository's environment. Without it the parser returns
# None and the resolver falls back to naming every path in the command, so
# running the driver under the repository venv checked the fallback and
# reported the change broken.
GATE_PYTHON = Path("/opt/claude-code-enforcement/venv/bin/python")
POLICY = ENFORCEMENT / "rules-policy.json"

DRIVER = '''
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import checkers
import enforcement_guard

base = Path(sys.argv[2])
edited = base / "a.py"
out = base / "out.txt"
policy = sys.argv[3]


def target(command):
    payload = {"tool_name": "Bash", "cwd": str(base), "tool_input": {"command": command}}
    return checkers._target_path(payload)


assert hasattr(enforcement_guard, "group_reads"), "the guard lost group_reads"

assert target(f"grep -c x {policy}") is None, "reading the policy is not a write"
assert target(f"ls -l {edited} 2>&1") is None, "stderr redirection is not a file write"
assert target(f"grep -A6 x {policy} | sed 's/a/b/' | head -20") is None, "a read pipeline"
assert target(f"grep -o x {edited} > {out}") == out, "the redirect target is the write"
assert target(f"echo y >> {edited}") == edited, "appending is a write"
assert target(f"python3 - <<PY\\nopen('{edited}', 'w')\\nPY") == edited, "heredoc is a write"

explicit = {"tool_name": "Edit", "tool_input": {"file_path": str(edited)}}
assert checkers._target_path(explicit) == edited, "an explicit file_path still wins"

print("ok")
'''


def _patcher():
    spec = importlib.util.spec_from_file_location("apply_bash_write_precision", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def patcher():
    if not SCRIPT.is_file():
        pytest.skip("the apply script is not present")
    return _patcher()


@pytest.fixture(scope="module")
def sources():
    """The installed checkers and guard, or a skip when the gate is absent."""
    checkers = ENFORCEMENT / "checkers.py"
    guard = ENFORCEMENT / "enforcement_guard.py"
    if not checkers.is_file() or not guard.is_file():
        pytest.skip("the enforcement gate is not installed on this machine")
    if not GATE_PYTHON.exists():
        pytest.skip("the gate interpreter is not present")
    return checkers.read_text(encoding="utf-8"), guard.read_text(encoding="utf-8")


def _patched(patcher, sources) -> tuple[str, str]:
    """Both patched texts, or a skip when the change is already applied."""
    checkers_text, guard_text = sources
    if "_redirect_targets" in checkers_text:
        pytest.skip("the change is already applied")
    patched_checkers = patcher._patched_checkers(checkers_text)
    patched_guard = patcher._patched_guard(guard_text)
    assert patched_checkers is not None, "the checkers block no longer matches"
    assert patched_guard is not None, "the guard fragments no longer match"
    return patched_checkers, patched_guard


def _patched_tree(patcher, sources, tmp_path: Path) -> Path:
    """A copy of the enforcement tree with both files patched."""
    tree = tmp_path / "enforcement"
    shutil.copytree(ENFORCEMENT, tree)
    patched_checkers, patched_guard = _patched(patcher, sources)
    (tree / "checkers.py").write_text(patched_checkers, encoding="utf-8")
    (tree / "enforcement_guard.py").write_text(patched_guard, encoding="utf-8")
    return tree


def test_the_patched_resolver_names_the_written_file(patcher, sources, tmp_path):
    """Five decisions the text-scanning version got wrong or could not make."""
    tree = _patched_tree(patcher, sources, tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.py").write_text("x = 1\n", encoding="utf-8")
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER, encoding="utf-8")

    finished = subprocess.run(
        [str(GATE_PYTHON), str(driver), str(tree), str(work), str(POLICY)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert finished.returncode == 0, finished.stderr[-2000:]
    assert finished.stdout.strip().endswith("ok")


def test_applying_twice_is_refused(patcher, sources):
    """Running it again must not stack a second copy of either change."""
    patched_checkers, patched_guard = _patched(patcher, sources)

    assert patcher._patched_checkers(patched_checkers) is None
    assert patcher._patched_guard(patched_guard) is None


def test_a_source_it_was_not_written_against_is_refused(patcher):
    """A partial match means the file is not what the patch expects."""
    assert patcher._patched_checkers("nothing to anchor on\n") is None
    assert patcher._patched_guard("nothing to anchor on\n") is None
