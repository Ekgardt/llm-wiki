"""Discovering the managed Pyright profile never imports or calls the installer."""

from __future__ import annotations

import ast
from pathlib import Path

import pyright_profile


def test_profile_discovery_does_not_import_or_call_installer() -> None:
    source = Path(pyright_profile.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert not any(
        isinstance(node, (ast.Import, ast.ImportFrom))
        and (
            any(alias.name == "install_pyright" for alias in node.names)
            if isinstance(node, ast.Import)
            else node.module == "install_pyright"
        )
        for node in ast.walk(tree)
    )
