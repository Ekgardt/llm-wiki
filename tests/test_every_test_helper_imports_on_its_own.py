"""Every shared test module imports in a fresh interpreter, whichever test file runs first.

A helper split out of a test file must carry the import-path setup it needs: when the
setup stays behind in the original file, the helper imports only in runs where that
file happened to load first, and a shard or a single-file run fails at collection.
"""
from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTEST_IMPORT_ROOTS = (ROOT, ROOT / "scripts")
INDEXER_INPUT_DATA = Path("tests/fixtures")
IMPORT_ALONE = "import importlib, sys; sys.path[0:0] = sys.argv[2:]; importlib.import_module(sys.argv[1])"


def _shared_test_modules() -> list[str]:
    tracked = subprocess.run(["git", "ls-files", "--", "tests/*.py"], cwd=ROOT, capture_output=True,
                             text=True, check=True).stdout.split()
    shared = [Path(path) for path in tracked if not _is_collected_package_or_data(Path(path))]
    return [".".join(path.with_suffix("").parts) for path in shared]


def _is_collected_package_or_data(path: Path) -> bool:
    if path.is_relative_to(INDEXER_INPUT_DATA):
        return True
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
