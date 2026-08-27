"""The workspace symbol walk is bounded: no agent worktrees, no retained trees.

NEW-110: ``build_python_symbol_registry`` walked every ``*.py`` under the
workspace — 7,549 files on this vault, 7,215 of them under ``.claude/`` agent
worktrees — and retained every parsed AST for a re-export pass, measured at
~1.5 GiB RSS per 1,000 files (MemoryError at 2,597 files under a 4 GiB cap).
The walk now prunes hidden directories the way the corpus walker already does
(which is why the graph index itself never indexes ``.claude``), plus the
workspace cache names ``code_graph`` already skips, and the re-export pass
keeps extracted records instead of trees.
"""

from __future__ import annotations

import ast
import gc
import sys
import weakref
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import import_resolver  # noqa: E402  (needs the scripts/ path above)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_the_walk_does_not_descend_into_hidden_directories(tmp_path):
    """Agent worktrees under ``.claude/`` must never enter the registry."""
    _write(tmp_path / "pkg" / "real.py", "def real_symbol(): pass\n")
    _write(
        tmp_path / ".claude" / "worktrees" / "agent" / "pkg" / "ghost.py",
        "def ghost_symbol(): pass\n",
    )
    _write(tmp_path / ".hidden" / "other.py", "def hidden_symbol(): pass\n")

    registry = import_resolver.build_python_symbol_registry(tmp_path)

    assert "pkg.real.real_symbol" in registry.symbols
    assert not any("ghost" in symbol for symbol in registry.symbols)
    assert not any("hidden_symbol" in symbol for symbol in registry.symbols)
    assert not any("ghost" in module for module in registry.modules)


def test_the_walk_skips_workspace_caches_but_not_a_hidden_root(tmp_path):
    """``node_modules`` and friends are pruned; a dot-named root still works."""
    root = tmp_path / ".claude" / "worktrees" / "agent"
    _write(root / "pkg" / "mine.py", "def mine(): pass\n")
    _write(root / "node_modules" / "dep" / "setup.py", "def dep(): pass\n")
    _write(root / "venv" / "lib" / "site.py", "def site(): pass\n")
    _write(root / "__pycache__" / "stale.py", "def stale(): pass\n")

    registry = import_resolver.build_python_symbol_registry(root)

    assert "pkg.mine.mine" in registry.symbols
    assert not any("dep" in symbol for symbol in registry.symbols)
    assert not any(".site" in symbol for symbol in registry.symbols)
    assert not any("stale" in symbol for symbol in registry.symbols)


def test_parsed_trees_are_dropped_before_the_next_file_is_parsed(tmp_path, monkeypatch):
    """The registry keeps extracted records, never a growing list of ASTs."""
    for index in range(12):
        _write(tmp_path / "pkg" / f"module_{index:02d}.py", "def handler(): pass\n")
    _write(
        tmp_path / "pkg" / "__init__.py",
        "from pkg.module_00 import handler\n",
    )
    live_trees: list[weakref.ref] = []
    peak_prior_alive = 0
    real_parse = ast.parse

    def tracking_parse(*args, **kwargs):
        nonlocal peak_prior_alive
        gc.collect()
        alive = sum(1 for reference in live_trees if reference() is not None)
        peak_prior_alive = max(peak_prior_alive, alive)
        tree = real_parse(*args, **kwargs)
        live_trees.append(weakref.ref(tree))
        return tree

    monkeypatch.setattr(import_resolver.ast, "parse", tracking_parse)

    registry = import_resolver.build_python_symbol_registry(tmp_path)

    assert "pkg.handler" in registry.symbols  # re-exports still resolve
    assert len(live_trees) == 13
    assert peak_prior_alive <= 1, (
        f"{peak_prior_alive} earlier ASTs were still alive while parsing the "
        "next file; the registry must extract per-file records and drop trees"
    )


def test_reexport_chains_still_resolve_without_retained_trees(tmp_path):
    """The fixed-point pass over extracted records matches the old semantics."""
    _write(tmp_path / "pkg" / "inner.py", "def deep(): pass\nclass Thing:\n    def method(self): pass\n")
    _write(tmp_path / "pkg" / "__init__.py", "from pkg.inner import deep as shallow\n")
    _write(tmp_path / "top" / "__init__.py", "from pkg import shallow\n")

    registry = import_resolver.build_python_symbol_registry(tmp_path)

    assert "pkg.inner.deep" in registry.symbols
    assert "pkg.shallow" in registry.symbols
    assert "top.shallow" in registry.symbols
    assert "pkg.inner.Thing.method" in registry.symbols
