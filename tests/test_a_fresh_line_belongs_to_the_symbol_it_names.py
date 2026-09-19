"""Audit 3, A3/A4: a line or a block read from the file belongs to the symbol named.

Research: `docs/research/2026-09-17-a-fresh-line-belongs-to-the-symbol-it-names.md`.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

SOURCE = (
    "class First:\n"
    "    def run(self):\n"
    "        return 'first'\n"
    "\n"
    "\n"
    "class Second:\n"
    "    def run(self):\n"
    "        return 'second'\n"
    "\n"
    "\n"
    "def helper():\n"
    "    return 1\n"
    "\n"
    "\n"
    "def caller():\n"
    "    value = 0\n"
    "    return helper() + value\n"
)
# Three lines on top: every definition and every call site moves down by three.
# Three and not two, so the caller's new definition line (18) cannot be mistaken
# for its stored call site (17).
EDITED = "import os\nimport sys\nimport time\n" + SOURCE


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True)


def _repository(root: Path) -> Path:
    (root / "pkg").mkdir(parents=True)
    (root / "pkg/__init__.py").write_bytes(b"")
    (root / "pkg/core.py").write_bytes(SOURCE.encode("utf-8"))
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "test")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "initial")
    return root


@pytest.fixture(scope="module")
def stale(tmp_path_factory):
    """A repository indexed from SOURCE whose file now holds EDITED."""
    base = tmp_path_factory.mktemp("fresh-line")
    vault = base / "vault"
    (vault / "knowledge/notes").mkdir(parents=True)
    (vault / "knowledge/projects").mkdir(parents=True)
    state = base / "state"
    state.mkdir()
    patch = pytest.MonkeyPatch()
    patch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    patch.setenv("LLM_WIKI_ROOT", str(vault))
    import evidence_reader_cache
    import memory_state
    import repository_index

    patch.setattr(memory_state, "ROOT", vault, raising=False)
    patch.setattr(memory_state, "STATE_ROOT", state, raising=False)
    evidence_reader_cache.clear()
    repository = _repository(base / "repo")
    answer = repository_index.index_repository(repository, state_root=state)
    assert answer["status"] == "indexed", answer
    (repository / "pkg/core.py").write_bytes(EDITED.encode("utf-8"))
    yield repository
    evidence_reader_cache.clear()
    patch.undo()


def _request(repository: Path, symbol: str, depth=None) -> dict:
    return {
        "symbol": symbol,
        "resolved": repository,
        "live": False,
        "depth": depth,
        "deadline": time.monotonic() + 20,
    }


def test_a_one_hop_caller_row_keeps_its_call_site(stale):
    import mcp_server

    answer = mcp_server._architecture_callers(_request(stale, "helper"))
    rows = [(row["qualified_name"], row["line"], "line_read_from" in row) for row in answer["callers"]]
    assert rows == [("pkg.core.caller", 17, False)]


def test_a_walked_caller_row_gets_the_definition_line_on_disk(stale):
    import mcp_server

    answer = mcp_server._architecture_callers(_request(stale, "helper", depth=2))
    rows = [(row["qualified_name"], row["line"], row.get("line_read_from")) for row in answer["callers"]]
    assert rows == [("pkg.core.caller", 18, "file")]


def test_a_stale_method_answers_with_its_own_class_block(stale):
    from symbol_snippet import snippet_for_symbol

    answer = snippet_for_symbol(stale, "Second.run", time.monotonic() + 20)
    blocks = [(item["start_line"], item["source"]) for item in answer["snippets"]]
    assert blocks == [(10,"    def run(self):\n        return 'second'")]


def test_a_method_that_left_the_file_never_borrows_a_namesake(tmp_path):
    import fresh_positions

    path = tmp_path / "module.py"
    path.write_text("class First:\n    def run(self):\n        return 1\n", encoding="utf-8")
    spans = (
        fresh_positions.span_of(path, "pkg.module.Second.run", member=True),
        fresh_positions.span_of(path, "pkg.module.First.run", member=True),
    )
    assert spans == (None, (2, 3))


def test_a_module_level_function_owns_its_bare_name(tmp_path):
    import fresh_positions

    path = tmp_path / "module.py"
    path.write_text(
        "class Holder:\n    def run(self):\n        return 1\n\n\ndef run():\n    return 2\n",
        encoding="utf-8",
    )
    rows = [{"file": "module.py", "line": 1, "qualified_name": "module.run"}]
    found = (
        fresh_positions.span_of(path, "module.run"),
        fresh_positions.refreshed_rows(rows, tmp_path)[0]["line"],
    )
    assert found == ((6, 7), 6)
