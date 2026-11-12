"""Every shared test module imports in a fresh interpreter, whichever test file runs first.

A helper split out of a test file must carry the import-path setup it needs: when the
setup stays behind in the original file, the helper imports only in runs where that
file happened to load first, and a shard or a single-file run fails at collection.
"""
from __future__ import annotations

import ast
import importlib.util
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTEST_IMPORT_ROOTS = (ROOT, ROOT / "scripts")
INDEXER_INPUT_DATA = Path("tests/fixtures")
IMPORT_ALONE = "import importlib, sys; sys.path[0:0] = sys.argv[2:]; importlib.import_module(sys.argv[1])"


def _tracked_test_sources() -> list[Path]:
    tracked = subprocess.run(["git", "ls-files", "--", "tests/*.py"], cwd=ROOT, capture_output=True,
                             text=True, check=True).stdout.split()
    return [Path(path) for path in tracked if not Path(path).is_relative_to(INDEXER_INPUT_DATA)]


def _names_one_import_brings(node: ast.AST, package: str) -> set[str]:
    if isinstance(node, ast.Import):
        return {alias.name for alias in node.names}
    if isinstance(node, ast.ImportFrom):
        return _names_from_import(node, package)
    return set()


def _names_from_import(node: ast.ImportFrom, package: str) -> set[str]:
    """`from pkg import name` may bring the module `pkg.name`; a relative `pkg` is resolved in its package."""
    base = importlib.util.resolve_name("." * node.level + (node.module or ""), package)
    return {base, *(f"{base}.{alias.name}" for alias in node.names)}


def _names_imported_by(source: Path, text: str) -> set[str]:
    """A bare name may also reach a helper through the tests directory on the import path."""
    package = ".".join(source.parent.parts)
    names = set().union(*(_names_one_import_brings(node, package) for node in ast.walk(ast.parse(text))))
    return names | {f"tests.{name}" for name in names}


def _imported_names(source: Path) -> set[str]:
    return _names_imported_by(source, (ROOT / source).read_text(encoding="utf-8"))


def _shared_test_modules() -> list[str]:
    """Helpers other test files import; a suite run only by its own path is not shared."""
    sources = _tracked_test_sources()
    imported = set().union(*map(_imported_names, sources))
    helpers = [".".join(path.with_suffix("").parts) for path in sources if not _is_collected_or_package(path)]
    return [module for module in helpers if module in imported]


def _is_collected_or_package(path: Path) -> bool:
    return path.name.startswith("test_") or path.name in {"conftest.py", "__init__.py"}


def _import_failure(module: str, roots: tuple[Path, ...] = PYTEST_IMPORT_ROOTS) -> str | None:
    done = subprocess.run([sys.executable, "-I", "-c", IMPORT_ALONE, module, *map(str, roots)],
                          cwd=ROOT, capture_output=True, text=True, timeout=120, check=False,
                          stdin=subprocess.DEVNULL)
    if done.returncode == 0:
        return None
    return f"{module}: {done.stderr.strip().splitlines()[-1]}"


def test_the_guard_sees_the_shared_modules() -> None:
    modules = _shared_test_modules()
    assert {"tests.code_navigation_benchmark_helpers", "tests.modules.flush_corpus_labels.contract_helpers"} <= set(modules)
    assert not [module for module in modules if module.endswith(".parquet_qualification")]
    assert "tests.fake_process_tree" in modules


def test_relative_and_bare_imports_name_the_helper_they_reach() -> None:
    source = Path("tests/modules/split_suite/test_split.py")
    text = "from .helper import f\nfrom . import sibling\nfrom ..up import g\nfrom bare_helper import h\n"
    names = _names_imported_by(source, text)
    assert {"tests.modules.split_suite.helper", "tests.modules.split_suite.sibling",
            "tests.modules.up", "tests.bare_helper"} <= names


def test_a_helper_without_its_path_setup_is_named(tmp_path: Path) -> None:
    helper = tmp_path / "orphan_helper.py"
    helper.write_text("import flush_corpus_labels\n", encoding="utf-8")
    failure = _import_failure("orphan_helper", (tmp_path,))
    assert failure == "orphan_helper: ModuleNotFoundError: No module named 'flush_corpus_labels'"


def test_a_helper_that_imports_is_not_named(tmp_path: Path) -> None:
    (tmp_path / "sound_helper.py").write_text("import json\n", encoding="utf-8")
    assert _import_failure("sound_helper", (tmp_path,)) is None


def test_every_shared_test_module_imports_in_a_fresh_interpreter() -> None:
    with ThreadPoolExecutor(max_workers=os.cpu_count()) as pool:
        failures = [failure for failure in pool.map(_import_failure, _shared_test_modules()) if failure]
    assert failures == []
