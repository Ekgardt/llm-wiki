"""Import checks for already-independent stdlib-only product modules."""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

from tests.slow_machine import LONG_TIMEOUT


def _imports(node):
    if isinstance(node, ast.ImportFrom):
        return {(node.module or '', node.level)}
    if isinstance(node, ast.Import):
        return {(a.name, 0) for a in node.names}
    return set()


def nonstdlib_imports(source: str) -> set[str]:
    imports = set().union(*(_imports(n) for n in ast.walk(ast.parse(source))))
    return {name for name, level in imports if level or name.split('.')[0] not in sys.stdlib_module_names}


def assert_stdlib_boundary(root: Path, module: str) -> None:
    source = (root / 'scripts' / f'{module}.py').read_text()
    assert not nonstdlib_imports(source)


def assert_isolated_import(root: Path, module: str, *, dependencies=()) -> None:
    program = '''
import importlib, importlib.abc, json, pathlib, sys
scripts = pathlib.Path(sys.argv[1])
module = sys.argv[2]
allowed = set(json.loads(sys.argv[3]))
local = {p.stem for p in scripts.glob('*.py')} | {'llm_wiki'}
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        assert fullname in allowed or fullname.split('.')[0] not in local, fullname
sys.path.insert(0, str(scripts))
sys.meta_path.insert(0, Guard())
importlib.import_module(module)
'''
    result = subprocess.run([sys.executable, '-c', program, str(root / 'scripts'), module,
                             json.dumps([module, *dependencies])],
                            capture_output=True, text=True, timeout=LONG_TIMEOUT)
    assert result.returncode == 0, result.stderr
