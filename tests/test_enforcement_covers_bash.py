"""Rules 1 and 2 can see a file a shell command writes.

The gap these cover is the one that let a whole session of edits through: every
one arrived as a heredoc inside `Bash`, and both checkers start from
`tool_input.file_path`, which a `Bash` payload does not carry. Skips where the
gate is not installed.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ENFORCEMENT = Path("/etc/claude-code/enforcement")


def _installed_checkers():
    """The installed checkers module, or None when the gate is not there."""
    if not (ENFORCEMENT / "checkers.py").is_file():
        return None
    if str(ENFORCEMENT) not in sys.path:
        sys.path.insert(0, str(ENFORCEMENT))
    return importlib.import_module("checkers")


@pytest.fixture(scope="module")
def checkers():
    module = _installed_checkers()
    if module is None:
        pytest.skip("the enforcement gate is not installed on this machine")
    if not hasattr(module, "_bash_write_targets"):
        pytest.skip("the Bash coverage patch has not been applied")
    return module


def _heredoc_payload(target: str, cwd: str) -> dict:
    """The shape of the shell edit that walked around rules 1 and 2."""
    command = (
        "python3 - <<'PY'\n"
        "from pathlib import Path\n"
        f'p = Path("{target}")\n'
        'p.write_text(p.read_text() + "\\n")\n'
        "PY"
    )
    return {"tool_name": "Bash", "cwd": cwd, "tool_input": {"command": command}}


def test_a_heredoc_that_writes_a_file_now_has_a_target(checkers, tmp_path):
    """Without a target both checkers return None and the rule never fires."""
    written = tmp_path / "module.py"
    written.write_text("x = 1\n", encoding="utf-8")

    payload = _heredoc_payload(written.name, str(tmp_path))

    assert checkers._target_path(payload) == written


def _shell_parses() -> bool:
    """Whether the parser works in this process, not just on the gate.

    Telling a read from a write needs bashlex, which is installed for the
    gate's own interpreter and not for this one. Without it the resolver falls
    back to naming every path, which is the fail-closed answer and not the
    behaviour this checks.
    """
    import shell_ast

    return shell_ast.parse_groups("true") is not None


def test_a_command_that_only_reads_has_no_target(checkers, tmp_path):
    """Reading is not changing code, and gating it would make the gate a nuisance."""
    if not _shell_parses():
        pytest.skip("bashlex is not importable here, so the fallback would be graded")
    read_only = tmp_path / "module.py"
    read_only.write_text("x = 1\n", encoding="utf-8")
    payload = {
        "tool_name": "Bash",
        "cwd": str(tmp_path),
        "tool_input": {"command": f"grep -n x {read_only.name}"},
    }

    assert checkers._target_path(payload) is None


def test_a_redirection_into_a_file_is_a_write(checkers, tmp_path):
    """The other spelling of the same bypass."""
    written = tmp_path / "module.py"
    written.write_text("x = 1\n", encoding="utf-8")
    payload = {
        "tool_name": "Bash",
        "cwd": str(tmp_path),
        "tool_input": {"command": f"echo 'y = 2' >> {written.name}"},
    }

    assert checkers._target_path(payload) == written


def test_an_explicit_file_path_still_wins(checkers, tmp_path):
    """An Edit payload must keep behaving exactly as it did before."""
    payload = {"tool_name": "Edit", "tool_input": {"file_path": str(tmp_path / "a.py")}}

    assert checkers._target_path(payload) == tmp_path / "a.py"
