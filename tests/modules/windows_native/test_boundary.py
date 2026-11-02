"""Native declarations are independent of the transaction executor."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
NAMES = {'_WindowsFileInformation', '_WindowsDispositionInformation', '_windows_kernel32'}


def test_executor_no_longer_defines_native_binding():
    tree = ast.parse((ROOT / 'scripts/markdown_transaction.py').read_text())
    names = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    assert not names.intersection(NAMES)


def test_owner_imports_only_stdlib():
    tree = ast.parse((ROOT / 'scripts/llm_wiki/windows_native.py').read_text())
    sources = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert sources <= {'__future__', 'ctypes'}


def test_plain_imports_are_stdlib():
    tree = ast.parse((ROOT / 'scripts/llm_wiki/windows_native.py').read_text())
    sources = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert sources <= {'ctypes', 'functools'}
