"""The hardened guard enforces the obligation, checked before it is applied.

`docs/enforcement/apply-guard-hardening.py` changes a live gate. This builds the
patched sources into a temporary copy of the enforcement tree, points the gate
at a temporary log directory, and drives the real `gate_stop.py` through a whole
turn: refuse, refuse, refuse, then yield with the give-up recorded.

Skips where the gate is not installed.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "docs" / "enforcement" / "apply-guard-hardening.py"
ENFORCEMENT = Path("/etc/claude-code/enforcement")
GATE_PYTHON = Path("/opt/claude-code-enforcement/venv/bin/python")
SESSION = "test-session"

CHECKERS_DRIVER = '''
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import checkers

notes = Path(sys.argv[2])
today = sys.argv[3]

assert checkers._is_new_module(notes / "brand-new.py", {"tool_name": "Bash"})
assert checkers._is_new_module(notes / "brand-new.py", {"tool_name": "Write"})
assert not checkers._is_new_module(notes / "brand-new.py", {"tool_name": "Read"})

assert not checkers._is_research(notes / "stub.md", today), "a dated stub is not research"
assert not checkers._is_research(notes / "long-no-source.md", today), "no source cited"
assert not checkers._is_research(notes / "sourced-but-old.md", today), "not dated today"
assert checkers._is_research(notes / "real.md", today), "a real note must pass"

print("ok")
'''


def _patcher():
    spec = importlib.util.spec_from_file_location("apply_guard_hardening", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def patcher():
    if not SCRIPT.is_file():
        pytest.skip("the apply script is not present")
    if not ENFORCEMENT.is_dir() or not GATE_PYTHON.exists():
        pytest.skip("the enforcement gate is not installed on this machine")
    return _patcher()


def _patched_tree(patcher, tmp_path: Path) -> Path:
    """A copy of the enforcement tree with every file patched, or a skip."""
    tree = tmp_path / "enforcement"
    shutil.copytree(ENFORCEMENT, tree)
    for path in patcher.TOUCHED:
        text = path.read_text(encoding="utf-8")
        if patcher.MARKERS[path] in text:
            pytest.skip("the change is already applied")
        patched = patcher._patched(path, text)
        assert patched is not None, f"{path.name} no longer matches the patch"
        (tree / path.name).write_text(patched, encoding="utf-8")
    return tree


def _audit(log_dir: Path, tool: str, subject: str) -> None:
    """Append one decision the turn-end gate will read."""
    record = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
        "tool": tool,
        "subject": subject,
        "decision": "allow",
    }
    with (log_dir / "audit.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record) + "\n")


def _run_gate(tree: Path, log_dir: Path) -> subprocess.CompletedProcess:
    environment = {**os.environ, "CLAUDE_RULES_LOG_DIR": str(log_dir)}
    return subprocess.run(
        [str(GATE_PYTHON), str(tree / "gate_stop.py")],
        input=json.dumps({"session_id": SESSION}),
        capture_output=True,
        text=True,
        env=environment,
        timeout=120,
    )


def _decisions(log_dir: Path) -> list[str]:
    lines = (log_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line)["decision"] for line in lines if line.strip()]


@pytest.fixture()
def turn(patcher, tmp_path):
    """A patched tree and a log directory holding one unverified code change."""
    tree = _patched_tree(patcher, tmp_path)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    _audit(log_dir, "Edit", str(Path(__file__).resolve()))
    return tree, log_dir


def _exhaust(tree: Path, log_dir: Path, bound: int = 3) -> list[int]:
    """Every refusal up to the bound, in order."""
    return [_run_gate(tree, log_dir).returncode for _ in range(bound)]


def test_the_refusal_repeats_while_the_work_stands_unverified(turn):
    """Refusing once and permitting after was the whole defect."""
    tree, log_dir = turn

    assert _exhaust(tree, log_dir) == [2, 2, 2]


def test_the_gate_gives_up_at_the_bound_and_records_it(turn):
    """An obligation that cannot be discharged must not wedge the session."""
    tree, log_dir = turn
    _exhaust(tree, log_dir)

    last = _run_gate(tree, log_dir)

    assert last.returncode == 0 and "YIELDED" in last.stderr
    assert "yield" in _decisions(log_dir), "the give-up has to be evidence, not silence"


def test_running_a_verifier_discharges_the_obligation(turn):
    """The obligation is discharged by work, not by asking again."""
    tree, log_dir = turn
    assert _run_gate(tree, log_dir).returncode == 2

    _audit(log_dir, "Bash", "pytest -q")
    finished = _run_gate(tree, log_dir)

    assert finished.returncode == 0
    assert "yield" not in _decisions(log_dir)


def test_a_turn_that_changed_nothing_passes(patcher, tmp_path):
    """The gate must stay silent on a turn with no code change in it."""
    tree = _patched_tree(patcher, tmp_path)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    assert _run_gate(tree, log_dir).returncode == 0


def _research_fixtures(notes: Path, today: str) -> None:
    """Four notes: three that must not count as research, and one that must."""
    sourced = "Body. " * 100 + "\n\nSee https://example.org/paper.pdf\n"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "stub.md").write_text(f"# {today}\n", encoding="utf-8")
    (notes / "long-no-source.md").write_text(f"# {today}\n" + "Body. " * 100, encoding="utf-8")
    (notes / "sourced-but-old.md").write_text("# 2020-01-01\n" + sourced, encoding="utf-8")
    (notes / "real.md").write_text(f"# {today}\n" + sourced, encoding="utf-8")


def test_the_new_triggers_and_the_research_predicate(patcher, tmp_path):
    """A shell-created module counts, and a dated stub stops counting."""
    tree = _patched_tree(patcher, tmp_path)
    notes = tmp_path / "notes"
    today = time.strftime("%Y-%m-%d")
    _research_fixtures(notes, today)
    driver = tmp_path / "driver.py"
    driver.write_text(CHECKERS_DRIVER, encoding="utf-8")

    finished = subprocess.run(
        [str(GATE_PYTHON), str(driver), str(tree), str(notes), today],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert finished.returncode == 0, finished.stderr[-2000:]


def test_applying_twice_is_refused(patcher, tmp_path):
    """Running it again must not stack a second copy of any change."""
    tree = _patched_tree(patcher, tmp_path)

    for path in patcher.TOUCHED:
        patched = (tree / path.name).read_text(encoding="utf-8")
        assert patcher._patched(path, patched) is None


def test_a_source_it_was_not_written_against_is_refused(patcher):
    """A partial match means the file is not what the patch expects."""
    for path in patcher.TOUCHED:
        assert patcher._patched(path, "nothing to anchor on\n") is None
