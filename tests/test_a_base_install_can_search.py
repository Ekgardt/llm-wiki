"""A module imported at the top of a script is installed by the base dependencies.

`corpus_snapshot` imported `yaml` and `fact_keys`/`evidence_pruning` imported `numpy`
at module level, while a base install (`--no-default-groups`, no extras) had neither:
20 modules failed to import and `recall` failed. Research:
`docs/research/2026-09-14-a-base-install-can-search.md`.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
# An import name that differs from the distribution that provides it.
DISTRIBUTION_OF = {"yaml": "pyyaml"}


def _base_distributions() -> set[str]:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        requirements = tomllib.load(stream)["project"]["dependencies"]
    return {re.split(r"[<>=!~;\[ ]", requirement, maxsplit=1)[0].lower() for requirement in requirements}


def _imported_names(node: ast.stmt) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        return [node.module or ""] * (node.level == 0)
    return []


def _top_level_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = [name for node in tree.body for name in _imported_names(node)]
    return {name.split(".")[0] for name in names if name}


def _local_modules() -> set[str]:
    return {path.stem for path in SCRIPTS.glob("*.py")} | {path.name for path in SCRIPTS.iterdir()}


def _third_party_imports() -> set[str]:
    everything = set().union(*map(_top_level_modules, SCRIPTS.glob("*.py")))
    return everything - (sys.stdlib_module_names | _local_modules() | {"__future__"})


def _distribution(module: str) -> str:
    return DISTRIBUTION_OF.get(module, module).lower()


def test_every_top_level_import_is_a_base_dependency():
    base = _base_distributions()

    assert sorted(set(map(_distribution, _third_party_imports())) - base) == []
