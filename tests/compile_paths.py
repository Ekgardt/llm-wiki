"""Point every module of the compile pipeline at one test vault."""
from __future__ import annotations

import importlib

from tests.script_sources import SCRIPTS, split_module_files

PIPELINE_MODULES = tuple(
    path.relative_to(SCRIPTS).with_suffix("").as_posix().replace("/", ".")
    for path in split_module_files("compile_pipeline", "compile_pipeline")
)
_COMPILE_MODULES = (
    "maybe_compile",
    "compile_memory",
    "compile_receipts",
    "compile_leased_run",
    "compile_lock",
    *PIPELINE_MODULES,
)


def point_compile_path(patch, name: str, value: object) -> None:
    """Patch ``name`` wherever a compile module holds its own copy of it."""
    modules = [importlib.import_module(module) for module in _COMPILE_MODULES]
    holders = [module for module in modules if hasattr(module, name)]
    assert holders, f"no compile module holds {name}"
    for module in holders:
        patch.setattr(module, name, value)


def compile_pipeline_binds(name: str) -> bool:
    """Whether the facade or any part it is composed from still binds ``name``."""
    return any(hasattr(importlib.import_module(module), name) for module in PIPELINE_MODULES)
