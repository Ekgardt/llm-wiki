"""Native declarations are independent of the transaction executor."""
import ast
from pathlib import Path

from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]


read_source = context_reader(ROOT, ROOT / 'docs/modules/windows-native-context.json')


def test_owner_imports_only_stdlib():
    tree = ast.parse(read_source(ROOT / 'scripts/llm_wiki/windows_native.py'))
    sources = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert sources <= {'__future__', 'ctypes'}


def test_plain_imports_are_stdlib():
    tree = ast.parse(read_source(ROOT / 'scripts/llm_wiki/windows_native.py'))
    sources = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert sources <= {'ctypes', 'functools'}
