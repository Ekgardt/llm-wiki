"""Consumer integration assertions retained from leaf module qualification."""
import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.slow_machine import LONG_TIMEOUT

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def test_restricted_paths_filesystem_module_no_longer_defines_owner_function():
    tree = ast.parse((ROOT / 'scripts/reliable_memory.py').read_text(encoding='utf-8'))
    assert 'restricted_relative_path' not in {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}


def test_runtime_contracts_former_facade_has_no_second_implementation():
    tree = ast.parse((ROOT / 'scripts/reliable_memory.py').read_text(encoding='utf-8'))
    assert 'UnsafeStateRoot' not in {getattr(n, 'name', None) for n in tree.body}


def test_runtime_environment_former_facade_has_no_second_implementation():
    tree = ast.parse((ROOT / 'scripts/reliable_memory.py').read_text(encoding='utf-8'))
    assert '_windows_network_anchor' not in {getattr(n, 'name', None) for n in tree.body}


def test_runtime_fingerprints_former_facade_has_no_second_implementation():
    tree = ast.parse((ROOT / 'scripts/reliable_memory.py').read_text(encoding='utf-8'))
    assert '_fingerprint_metadata' not in {getattr(n, 'name', None) for n in tree.body}


def test_runtime_publication_former_facade_has_no_second_implementation():
    tree = ast.parse((ROOT / 'scripts/reliable_memory.py').read_text(encoding='utf-8'))
    assert '_publication_digest' not in {getattr(n, 'name', None) for n in tree.body}


def test_runtime_records_former_facade_has_no_second_implementation():
    tree = ast.parse((ROOT / 'scripts/reliable_memory.py').read_text(encoding='utf-8'))
    assert '_contained_runtime_metadata' not in {getattr(n, 'name', None) for n in tree.body}


def test_runtime_sync_former_facade_has_no_second_implementation():
    tree = ast.parse((ROOT / 'scripts/reliable_memory.py').read_text(encoding='utf-8'))
    assert 'fsync_file' not in {getattr(n, 'name', None) for n in tree.body}


_sqlite_contracts_NAMES = {'OperationalDatabaseContract', 'OperationalDatabaseContractError', 'MigrationStatement', 'quote_sqlite_identifier', '_require_positive_int', '_require_migration_text'}


def test_sqlite_contracts_durability_module_has_no_contract_definitions():
    tree = ast.parse((ROOT / 'scripts/reliable_memory.py').read_text(encoding='utf-8'))
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))]
    assert not _sqlite_contracts_NAMES.intersection(n.name for n in nodes)


def test_sqlite_deadlines_filesystem_module_no_longer_owns_control():
    tree = ast.parse((ROOT / 'scripts/reliable_memory.py').read_text(encoding='utf-8'))
    assert 'operational_deadline_active' not in {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}


def test_sqlite_io_facade_has_no_function_bodies():
    tree = ast.parse((ROOT / 'scripts/reliable_memory.py').read_text(encoding='utf-8'))
    assert not any(isinstance(n, ast.FunctionDef) for n in tree.body)


def test_sqlite_migrations_filesystem_module_no_longer_owns_control():
    tree = ast.parse((ROOT / 'scripts/reliable_memory.py').read_text(encoding='utf-8'))
    assert 'run_resumable_migration' not in {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}


def test_sqlite_pragmas_filesystem_module_no_longer_defines_owner_function():
    tree = ast.parse((ROOT / 'scripts/reliable_memory.py').read_text(encoding='utf-8'))
    assert '_apply_operational_pragmas' not in {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}


def test_sqlite_transactions_filesystem_module_no_longer_defines_transaction_control():
    tree = ast.parse((ROOT / 'scripts/reliable_memory.py').read_text(encoding='utf-8'))
    assert 'begin_immediate' not in {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}


_reliability_defaults_NAMES = frozenset(json.loads(((ROOT / 'tests/modules/reliability_defaults/test_boundary.py').parent / 'fixtures/legacy-pickle.json').read_text(encoding='utf-8'))['names'])


def _reliability_defaults_legacy_import(node):
    if not isinstance(node, ast.ImportFrom) or node.module != 'reliable_memory':
        return False
    return any(alias.name in _reliability_defaults_NAMES or alias.name == '*' for alias in node.names)


def _reliability_defaults_module_aliases(node):
    if not isinstance(node, ast.Import):
        return set()
    return {alias.asname or alias.name for alias in node.names if alias.name == 'reliable_memory'}


def _reliability_defaults_facade_aliases(tree):
    return set().union(*(_reliability_defaults_module_aliases(node) for node in ast.walk(tree)))


def _reliability_defaults_qualified_type(node, aliases):
    if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
        return False
    return node.value.id in aliases and node.attr in _reliability_defaults_NAMES


def _reliability_defaults_violations(source):
    tree = ast.parse(source)
    aliases = _reliability_defaults_facade_aliases(tree)
    return [node.lineno for node in ast.walk(tree) if _reliability_defaults_legacy_import(node) or _reliability_defaults_qualified_type(node, aliases)]


def test_reliability_defaults_consumers_import_defaults_from_their_owner():
    offenders = {}
    for path in (ROOT / 'scripts').rglob('*.py'):
        bad = _reliability_defaults_violations(path.read_text(encoding='utf-8'))
        if bad:
            offenders[str(path.relative_to(ROOT))] = bad
    assert not offenders, offenders


@pytest.mark.parametrize('source', ['from reliable_memory import DEFAULTS', 'from reliable_memory import ReliableMemoryDefaults', 'import reliable_memory as runtime; runtime.ReliableMemoryDefaults()', 'from reliable_memory import DEFAULTS as Failure', 'from reliable_memory import *', 'import reliable_memory; failure = reliable_memory.DEFAULTS', 'import reliable_memory as queue; failure = queue.DEFAULTS', 'def local():\n    from reliable_memory import DEFAULTS'])
def test_reliability_defaults_guard_detects_executor_dependency(source):
    assert _reliability_defaults_violations(source)


@pytest.mark.parametrize('source', ['from reliable_memory import open_operational_db', 'import reliable_memory as queue; queue.open_operational_db()', 'from llm_wiki.reliability_defaults import DEFAULTS'])
def test_reliability_defaults_guard_allows_operations_and_owned_types(source):
    assert not _reliability_defaults_violations(source)


def test_state_io_uses_existing_owners_without_loading_the_facade(tmp_path):
    program = """
import pathlib, sys
sys.path.insert(0, sys.argv[1])
import memory_state
from llm_wiki.runtime_publication import durable_publish_file
from llm_wiki.runtime_sync import fsync_directory
assert 'reliable_memory' not in sys.modules
assert memory_state.durable_publish_file is durable_publish_file
assert memory_state.fsync_directory is fsync_directory
path = pathlib.Path(sys.argv[2]) / 'written.txt'
memory_state.atomic_write(path, 'complete state')
assert path.read_text(encoding='utf-8') == 'complete state'
assert 'reliable_memory' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, '-c', program, str(SCRIPTS), str(tmp_path)],
        capture_output=True, text=True, timeout=LONG_TIMEOUT,
    )
    assert result.returncode == 0, result.stderr
