"""`code_graph.py --callers` answers from the index or says there is none (#24).

Before this change the CLI fell through to a whole-tree re-parse whenever the
repository had no generation, which took 300 s on a 1 026-file repository and
never said why. A missing index is a fact to state, and `--live` is the
explicit way to pay for the scan.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _run(*arguments: str, state_root: Path, cwd: Path) -> subprocess.CompletedProcess:
    environment = dict(os.environ)
    environment["LLM_WIKI_STATE_ROOT"] = str(state_root)
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "code_graph.py"), *arguments],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=cwd,
        env=environment,
    )


def test_callers_without_a_generation_names_the_index_instead_of_scanning(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "app.py").write_text("def callee():\n    pass\n\ndef caller():\n    callee()\n")

    completed = _run(str(repository), "--callers", "callee", state_root=state, cwd=tmp_path)

    assert (completed.returncode, "mode=index" in completed.stdout, "--live" in completed.stdout) == (
        2,
        True,
        True,
    )


def test_live_opts_into_the_scan_and_finds_the_caller(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "app.py").write_text("def callee():\n    pass\n\ndef caller():\n    callee()\n")

    completed = _run(
        str(repository), "--callers", "callee", "--live", state_root=state, cwd=tmp_path
    )

    assert (completed.returncode, "1 found" in completed.stdout) == (0, True)
