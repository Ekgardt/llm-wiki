"""The repository root and the top-level definitions of a source, shared by the legacy alias tests."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _definitions(path):
    tree = ast.parse((ROOT / path).read_text(encoding='utf-8'))
    return {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
