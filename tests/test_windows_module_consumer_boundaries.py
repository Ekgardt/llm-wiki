"""Consumer integration assertions retained from leaf module qualification."""
import ast
from pathlib import Path

from tests.modules.windows_acl.test_boundary import NAMES as _windows_acl_NAMES

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"




def test_windows_acl_executors_do_not_define_acl_implementation():
    for filename in ('memory_queue.py', 'markdown_transaction.py'):
        tree = ast.parse((ROOT / 'scripts' / filename).read_text(encoding='utf-8'))
        definitions = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
        assert not definitions.intersection(_windows_acl_NAMES)


def test_windows_acl_runtime_io_does_not_import_executors_for_permissions():
    tree = ast.parse((ROOT / 'scripts/reliable_memory.py').read_text(encoding='utf-8'))
    sources = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not sources.intersection({'markdown_transaction', 'memory_queue'})


_windows_native_NAMES = {'_WindowsFileInformation', '_WindowsDispositionInformation', '_windows_kernel32'}


def test_windows_native_executor_no_longer_defines_native_binding():
    tree = ast.parse((ROOT / 'scripts/markdown_transaction.py').read_text(encoding='utf-8'))
    names = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    assert not names.intersection(_windows_native_NAMES)
