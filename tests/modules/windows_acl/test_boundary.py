"""ACL ownership must not point back at operational executors."""
import ast
from pathlib import Path

from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]


read_source = context_reader(ROOT, ROOT / 'docs/modules/windows-acl-context.json')


def test_owner_imports_only_declared_dependencies():
    tree = ast.parse(read_source(ROOT / 'scripts/llm_wiki/windows_acl.py'))
    imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert imports <= {'__future__', 'pathlib', 'llm_wiki.windows_native'}


def test_plain_imports_are_stdlib():
    tree = ast.parse(read_source(ROOT / 'scripts/llm_wiki/windows_acl.py'))
    imports = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert imports <= {'codecs', 'getpass', 'os', 're', 'subprocess', 'sys'}
