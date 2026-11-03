"""Consumer integration assertions retained from leaf module qualification."""
import ast
import json
from pathlib import Path

import pytest

from tests.modules.queue_timing.test_boundary import NAMES as _queue_timing_NAMES

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _compile_answer_records_named_functions(tree, name):
    return [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name]


def test_compile_answer_records_retained_queue_answer_is_verified_without_importing_the_controller():
    tree = ast.parse((ROOT / 'scripts/memory_queue.py').read_text(encoding='utf-8'))
    functions = _compile_answer_records_named_functions(tree, '_verify_retained_compile_answer')
    assert len(functions) == 1
    imports = {n.module for n in ast.walk(functions[0]) if isinstance(n, ast.ImportFrom)}
    assert imports == {'llm_wiki.compile_answer_records'}


def test_queue_attempts_executor_no_longer_defines_owned_function():
    tree = ast.parse((ROOT / 'scripts/memory_queue.py').read_text(encoding='utf-8'))
    assert '_apply_failure_state' not in {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}


_queue_evidence_records_SOURCE = ROOT / 'scripts/llm_wiki/queue_evidence_records.py'


def test_queue_evidence_records_required_queue_calls_use_aliases_not_duplicate_implementations():
    tree = ast.parse((ROOT / 'scripts/memory_queue.py').read_text(encoding='utf-8'))
    names = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
    local = ast.parse(_queue_evidence_records_SOURCE.read_text(encoding='utf-8'))
    owned = {node.name for node in local.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
    assert not names & owned
    assert '_read_exported_rows' in names


def test_queue_heartbeat_executor_does_not_define_heartbeat_implementation():
    tree = ast.parse((ROOT / 'scripts/memory_queue.py').read_text(encoding='utf-8'))
    names = {node.name for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef))}
    assert not names.intersection({'_LeaseHeartbeat', '_SourceFenceHeartbeat', '_join_heartbeat_or_refuse'})


def test_queue_migration_plan_executor_no_longer_defines_migration_plan():
    tree = ast.parse((ROOT / 'scripts/memory_queue.py').read_text(encoding='utf-8'))
    names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert not names.intersection({'_queue_v2_migration_statements', '_queue_v2_reconciliation_complete', '_ordered_queue_v2_tasks'})


_queue_schema_NAMES = {'_QUEUE_V3_TABLE_SQL', '_QUEUE_V3_INDEX_SQL', '_QUEUE_V3_TRIGGER_SQL'}


def test_queue_schema_executor_does_not_define_schema():
    tree = ast.parse((ROOT / 'scripts/memory_queue.py').read_text(encoding='utf-8'))
    assigned = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)}
    assert not assigned.intersection(_queue_schema_NAMES)


_queue_schema_operations_NAMES = {'_normalized_sql', '_queue_v3_object_matches', '_queue_v3_statements', '_queue_v3_schema_complete', '_expected_queue_v3_schema', '_queue_schema_rows', '_drop_order_key', '_drop_schema_object', '_drop_unexpected_objects', '_table_is_populated', '_check_no_populated_rebuild', '_malformed_queue_v3_objects', '_repair_partial_queue_v3_schema', '_queue_v3_trigger_statements', '_migration_error'}


def test_queue_schema_operations_executor_no_longer_defines_schema_operations():
    tree = ast.parse((ROOT / 'scripts/memory_queue.py').read_text(encoding='utf-8'))
    names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert not names.intersection(_queue_schema_operations_NAMES)


_queue_sources_NAMES = {'_collect_identity_from_mapping', '_collect_source_identity_strings', '_source_identity_strings', '_distinct_source_identities', '_payload_identity_fields', '_identity_paths_or_daily', '_validated_failure_identity', '_require_canonical_daily_id', '_unsafe_link_shape', '_unsafe_link_path', '_link_pair', '_normalized_source_link', '_normalized_source_links', '_validate_source_identity', '_validate_failure_identity', '_source_identity_from_payload', '_require_single_source_identity', '_queue_v3_source_links'}


def test_queue_sources_executor_has_no_source_contract_definitions():
    tree = ast.parse((ROOT / 'scripts/memory_queue.py').read_text(encoding='utf-8'))
    names = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert not names.intersection(_queue_sources_NAMES)


_queue_task_records_SOURCE = ROOT / 'scripts/llm_wiki/queue_task_records.py'


def test_queue_task_records_required_queue_calls_use_aliases_not_duplicate_implementations():
    tree = ast.parse((ROOT / 'scripts/memory_queue.py').read_text(encoding='utf-8'))
    names = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
    local = ast.parse(_queue_task_records_SOURCE.read_text(encoding='utf-8'))
    owned = {node.name for node in local.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
    assert not names & owned
    assert '_read_exported_rows' in names




def test_queue_timing_executor_does_not_define_timing_functions():
    tree = ast.parse((ROOT / 'scripts/memory_queue.py').read_text(encoding='utf-8'))
    names = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert not names.intersection(_queue_timing_NAMES)


def _queue_validation_defined_functions(path):
    return {n.name for n in ast.parse(path.read_text(encoding='utf-8')).body if isinstance(n, ast.FunctionDef)}


def test_queue_validation_executor_no_longer_defines_owned_function():
    owned = _queue_validation_defined_functions(ROOT / 'scripts/llm_wiki/queue_validation.py')
    executor = _queue_validation_defined_functions(ROOT / 'scripts/memory_queue.py')
    assert not owned.intersection(executor)


def test_queue_worker_state_queue_has_no_second_worker_value_implementation():
    tree = ast.parse((ROOT / 'scripts/memory_queue.py').read_text(encoding='utf-8'))
    names = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    assert not names.intersection({'QueueRetryPolicy', '_ProcessorOutcome', '_WorkerProgress', 'processor_error_code'})


def test_queue_worker_state_capture_reason_conversion_does_not_import_queue_executor():
    tree = ast.parse((ROOT / 'scripts/flush_memory.py').read_text(encoding='utf-8'))
    function = next(n for n in tree.body if getattr(n, 'name', None) == '_capture_queue_failure')
    assert isinstance(function, ast.FunctionDef)
    imports = {n.module for n in ast.walk(function) if isinstance(n, ast.ImportFrom)}
    assert 'memory_queue' not in imports
    assert 'llm_wiki.queue_worker_state' in imports


_queue_contracts_NAMES = frozenset(json.loads(((ROOT / 'tests/modules/queue_contracts/test_boundary.py').parent / 'fixtures/legacy-pickle.json').read_text(encoding='utf-8'))['names'])


def _queue_contracts_legacy_import(node):
    if not isinstance(node, ast.ImportFrom) or node.module != 'memory_queue':
        return False
    return any(alias.name in _queue_contracts_NAMES or alias.name == '*' for alias in node.names)


def _queue_contracts_module_aliases(node):
    if not isinstance(node, ast.Import):
        return set()
    return {alias.asname or alias.name for alias in node.names if alias.name == 'memory_queue'}


def _queue_contracts_queue_aliases(tree):
    return set().union(*(_queue_contracts_module_aliases(node) for node in ast.walk(tree)))


def _queue_contracts_qualified_type(node, aliases):
    if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
        return False
    return node.value.id in aliases and node.attr in _queue_contracts_NAMES


def _queue_contracts_violations(source):
    tree = ast.parse(source)
    aliases = _queue_contracts_queue_aliases(tree)
    return [node.lineno for node in ast.walk(tree) if _queue_contracts_legacy_import(node) or _queue_contracts_qualified_type(node, aliases)]


def test_queue_contracts_consumers_import_queue_types_from_their_owner():
    offenders = {}
    for path in (ROOT / 'scripts').rglob('*.py'):
        bad = _queue_contracts_violations(path.read_text(encoding='utf-8'))
        if bad:
            offenders[str(path.relative_to(ROOT))] = bad
    assert not offenders, offenders


@pytest.mark.parametrize('source', ['from memory_queue import QueueFailure', 'from memory_queue import QueueFailure as Failure', 'from memory_queue import *', 'import memory_queue; failure = memory_queue.QueueFailure', 'import memory_queue as queue; failure = queue.QueueFailure', 'def local():\n    from memory_queue import QueueFailure'])
def test_queue_contracts_guard_detects_executor_dependency(source):
    assert _queue_contracts_violations(source)


@pytest.mark.parametrize('source', ['from memory_queue import MemoryQueue', 'import memory_queue as queue; queue.active_or_legacy_memory_queue()', 'from llm_wiki.queue_contracts import QueueFailure'])
def test_queue_contracts_guard_allows_operations_and_owned_types(source):
    assert not _queue_contracts_violations(source)


def test_queue_resolution_uses_the_call_lifecycle_owner():
    tree = ast.parse((ROOT / 'scripts/memory_queue.py').read_text(encoding='utf-8'))
    imports = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    resolvers = _call_resolver_imports(imports)
    assert resolvers == ['llm_wiki.compile_call_runtime', 'llm_wiki.compile_call_runtime']
    assert not any(node.module == 'compile_controller' for node in imports)


def _call_resolver_imports(imports):
    return [node.module for node in imports
            if any(a.name == 'resolve_model_call' for a in node.names)]
