"""Consumer integration assertions retained from leaf module qualification."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


_windows_acl_NAMES = {'_acl_lines_naming', '_acl_is_owner_only', '_windows_acl_lines', '_acl_line_names_owner', '_acl_output_text', '_acl_owner_lines', '_acl_failure', '_windows_acl_identity', '_verify_no_other_acl', '_read_windows_acl', '_acl_entry_lines', '_verified_owner_acl_line', '_run_acl_command', '_acl_output_encodings', '_names_identity', '_is_owner_only_windows', '_apply_windows_acl', '_harden_windows_acl', '_console_code_page', '_verify_windows_owner_acl', '_acl_permission', '_require_verified_windows_owner_acl'}


def test_windows_acl_executors_do_not_define_acl_implementation():
    for filename in ('memory_queue.py', 'markdown_transaction.py'):
        tree = ast.parse((ROOT / 'scripts' / filename).read_text())
        definitions = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
        assert not definitions.intersection(_windows_acl_NAMES)


def test_windows_acl_runtime_io_does_not_import_executors_for_permissions():
    tree = ast.parse((ROOT / 'scripts/reliable_memory.py').read_text())
    sources = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not sources.intersection({'markdown_transaction', 'memory_queue'})


_windows_native_NAMES = {'_WindowsFileInformation', '_WindowsDispositionInformation', '_windows_kernel32'}


def test_windows_native_executor_no_longer_defines_native_binding():
    tree = ast.parse((ROOT / 'scripts/markdown_transaction.py').read_text())
    names = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    assert not names.intersection(_windows_native_NAMES)
