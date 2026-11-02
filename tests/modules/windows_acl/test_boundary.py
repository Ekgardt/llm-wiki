"""ACL ownership must not point back at operational executors."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
NAMES = {'_acl_lines_naming', '_acl_is_owner_only', '_windows_acl_lines', '_acl_line_names_owner', '_acl_output_text', '_acl_owner_lines', '_acl_failure', '_windows_acl_identity', '_verify_no_other_acl', '_read_windows_acl', '_acl_entry_lines', '_verified_owner_acl_line', '_run_acl_command', '_acl_output_encodings', '_names_identity', '_is_owner_only_windows', '_apply_windows_acl', '_harden_windows_acl', '_console_code_page', '_verify_windows_owner_acl', '_acl_permission', '_require_verified_windows_owner_acl'}


def test_executors_do_not_define_acl_implementation():
    for filename in ('memory_queue.py', 'markdown_transaction.py'):
        tree = ast.parse((ROOT / 'scripts' / filename).read_text())
        definitions = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
        assert not definitions.intersection(NAMES)


def test_owner_imports_only_declared_dependencies():
    tree = ast.parse((ROOT / 'scripts/llm_wiki/windows_acl.py').read_text())
    imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert imports <= {'__future__', 'pathlib', 'llm_wiki.windows_native'}


def test_plain_imports_are_stdlib():
    tree = ast.parse((ROOT / 'scripts/llm_wiki/windows_acl.py').read_text())
    imports = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert imports <= {'codecs', 'getpass', 'os', 're', 'subprocess', 'sys'}


def test_runtime_io_does_not_import_executors_for_permissions():
    tree = ast.parse((ROOT / 'scripts/reliable_memory.py').read_text())
    sources = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not sources.intersection({'markdown_transaction', 'memory_queue'})
