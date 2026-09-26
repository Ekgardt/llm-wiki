"""Audit 3, A13/A14/B31/B32: insertions, large graphs, submodules and symlinks.

Research: `docs/research/2026-09-17-impact-reads-what-the-diff-really-holds.md`.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.slow_machine import SHORT_TIMEOUT

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

FILLERS = 60
CORE_LINES = [
    "def first():",
    "    return 1",
    "def second():",
    "    return 2",
    "",
    "",
    "def spaced():",
    "    return 3",
    "",
    "",
    "def after():",
    "    return 4",
]
CORE = "\n".join(CORE_LINES) + "\n"
TEST_MODULE = "from pkg.core import first\n\n\ndef test_first():\n    assert first() == 1\n"


def _fillers() -> str:
    return "".join(f"def filler_{index}():\n    return {index}\n\n\n" for index in range(FILLERS))


def _with_line(after_line: int, text: str) -> str:
    """CORE with `text` inserted after the 1-based line `after_line`."""
    lines = [*CORE_LINES[:after_line], text, *CORE_LINES[after_line:]]
    return "\n".join(lines) + "\n"


def _git(root: Path, *arguments: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(root), *arguments], check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


def _repository(root: Path) -> Path:
    (root / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pkg/__init__.py").write_bytes(b"")
    (root / "pkg/core.py").write_bytes(CORE.encode("utf-8"))
    (root / "pkg/fillers.py").write_bytes(_fillers().encode("utf-8"))
    (root / "tests/test_core.py").write_bytes(TEST_MODULE.encode("utf-8"))
    if hasattr(os, "symlink"):
        (root / "docs-link").symlink_to("pkg")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "test")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "initial")
    return root


@pytest.fixture(scope="module")
def indexed(tmp_path_factory):
    base = tmp_path_factory.mktemp("impact-diff")
    vault = base / "vault"
    (vault / "knowledge/notes").mkdir(parents=True)
    (vault / "knowledge/projects").mkdir(parents=True)
    state = base / "state"
    state.mkdir()
    patch = pytest.MonkeyPatch()
    patch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    patch.setenv("LLM_WIKI_ROOT", str(vault))
    import evidence_reader_cache
    import impact_analysis
    import memory_state
    import repository_index

    patch.setattr(memory_state, "ROOT", vault, raising=False)
    patch.setattr(memory_state, "STATE_ROOT", state, raising=False)
    patch.setattr(impact_analysis, "STATE_ROOT", state, raising=False)
    evidence_reader_cache.clear()
    repository = _repository(base / "repo")
    answer = repository_index.index_repository(repository, state_root=state)
    assert answer["status"] == "indexed", answer
    yield repository
    evidence_reader_cache.clear()
    patch.undo()


def _dirty_impact(repository: Path, text: str, **options) -> dict:
    """The impact of `pkg/core.py` holding `text`, with the file put back after."""
    import impact_analysis

    target = repository / "pkg/core.py"
    target.write_bytes(text.encode("utf-8"))
    try:
        return impact_analysis.analyze_impact(
            root=repository, comparison="dirty", deadline=time.monotonic() + SHORT_TIMEOUT, **options
        )
    finally:
        target.write_bytes(CORE.encode("utf-8"))


def _changed_names(impact: dict) -> list[str]:
    return [item["name"] for item in impact["changed_symbols"]]


def test_an_indented_line_appended_to_a_function_belongs_to_that_function(indexed):
    touching = _dirty_impact(indexed, _with_line(2, "    # appended to first"))
    spaced = _dirty_impact(indexed, _with_line(8, "    # appended to spaced"))
    assert (_changed_names(touching), _changed_names(spaced)) == (["first"], ["spaced"])


def test_an_unindented_insertion_still_belongs_to_what_follows_it(indexed):
    impact = _dirty_impact(indexed, _with_line(10, "# a comment above after"))
    assert _changed_names(impact) == ["after"]


def test_a_graph_larger_than_one_read_still_names_the_affected_test(indexed):
    import impact_analysis

    # The generation holds more traversed edges than this ceiling (one DEFINES
    # per filler alone), but far fewer point at any one round's frontier.
    limits = impact_analysis.ImpactLimits(max_graph_rows=FILLERS // 2)
    impact = _dirty_impact(indexed, CORE.replace("return 1", "return 10"), limits=limits)
    names = [item["name"] for item in impact["affected"]["tests"]]
    assert ("test_first" in names, impact["warnings"]) == (True, [])


def _commit_with_submodule_and_edit(repository: Path) -> tuple[str, str]:
    base = _git(repository, "rev-parse", "HEAD")
    _git(repository, "update-index", "--add", "--cacheinfo", f"160000,{base},vendor/sub")
    (repository / "pkg/core.py").write_bytes(CORE.replace("return 2", "return 20").encode("utf-8"))
    _git(repository, "add", "pkg/core.py")
    _git(repository, "commit", "-qm", "bump a submodule and edit a file")
    return base, _git(repository, "rev-parse", "HEAD")


def test_a_submodule_bump_does_not_discard_the_rest_of_the_diff(indexed):
    import impact_analysis

    base, target = _commit_with_submodule_and_edit(indexed)
    try:
        impact = impact_analysis.analyze_impact(
            root=indexed, comparison="two-commits", base=base, target=target,
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
    finally:
        _git(indexed, "reset", "-q", "--hard", base)
    paths = sorted(change["new_path"] for change in impact["changes"])
    assert (paths, _changed_names(impact)) == (["pkg/core.py", "vendor/sub"], ["second"])


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_a_changed_symlink_is_listed_and_the_rest_is_still_analysed(indexed):
    link = indexed / "docs-link"
    link.unlink()
    link.symlink_to("tests")
    try:
        impact = _dirty_impact(indexed, CORE.replace("return 3", "return 30"))
    finally:
        link.unlink()
        link.symlink_to("pkg")
    paths = sorted(change["new_path"] for change in impact["changes"])
    assert (paths, _changed_names(impact)) == (["docs-link", "pkg/core.py"], ["spaced"])
