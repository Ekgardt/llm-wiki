"""Consumer integration assertions retained from leaf module qualification."""
import ast
import json
import re
from pathlib import Path

import pytest

from tests.module_context import count_tokens
from tests.modules.queue_timing.boundary_helpers import timing_function_names
from tests.queue_engine_sources import queue_engine_files, queue_engine_tree

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _compile_answer_records_named_functions(tree, name):
    return [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name]


def test_compile_answer_records_retained_queue_answer_is_verified_without_importing_the_controller():
    tree = queue_engine_tree()
    functions = _compile_answer_records_named_functions(tree, '_verify_retained_compile_answer')
    assert len(functions) == 1
    imports = {n.module for n in ast.walk(functions[0]) if isinstance(n, ast.ImportFrom)}
    assert imports == {'llm_wiki.compile_answer_records'}


def test_queue_attempts_executor_no_longer_defines_owned_function():
    tree = queue_engine_tree()
    assert 'apply_failure_state' not in {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}


_queue_evidence_records_SOURCE = ROOT / 'scripts/llm_wiki/queue_evidence_records.py'


def test_queue_evidence_records_required_queue_calls_use_aliases_not_duplicate_implementations():
    tree = queue_engine_tree()
    names = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
    local = ast.parse(_queue_evidence_records_SOURCE.read_text(encoding='utf-8'))
    owned = {node.name for node in local.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
    assert not names & owned
    assert '_read_exported_rows' in names


def test_queue_heartbeat_executor_does_not_define_heartbeat_implementation():
    tree = queue_engine_tree()
    names = {node.name for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef))}
    assert not names.intersection({'LeaseHeartbeat', 'SourceFenceHeartbeat', '_join_heartbeat_or_refuse'})


def test_queue_migration_plan_executor_no_longer_defines_migration_plan():
    tree = queue_engine_tree()
    names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert not names.intersection({'queue_v2_migration_statements', 'queue_v2_reconciliation_complete', 'ordered_queue_v2_tasks'})


_queue_schema_NAMES = {'QUEUE_V3_TABLE_SQL', 'QUEUE_V3_INDEX_SQL', 'QUEUE_V3_TRIGGER_SQL'}


def test_queue_schema_executor_does_not_define_schema():
    tree = queue_engine_tree()
    assigned = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)}
    assert not assigned.intersection(_queue_schema_NAMES)


_queue_schema_operations_NAMES = {'normalized_sql', 'queue_v3_object_matches', 'queue_v3_statements', 'queue_v3_schema_complete', 'expected_queue_v3_schema', 'queue_schema_rows', 'drop_order_key', 'drop_schema_object', 'drop_unexpected_objects', 'table_is_populated', 'check_no_populated_rebuild', 'malformed_queue_v3_objects', 'repair_partial_queue_v3_schema', 'queue_v3_trigger_statements', 'migration_error'}


def test_queue_schema_operations_executor_no_longer_defines_schema_operations():
    tree = queue_engine_tree()
    names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert not names.intersection(_queue_schema_operations_NAMES)


_queue_sources_NAMES = {'collect_identity_from_mapping', 'collect_source_identity_strings', 'source_identity_strings', 'distinct_source_identities', 'payload_identity_fields', 'identity_paths_or_daily', 'validated_failure_identity', 'require_canonical_daily_id', 'unsafe_link_shape', 'unsafe_link_path', 'link_pair', 'normalized_source_link', 'normalized_source_links', 'validate_source_identity', 'validate_failure_identity', 'source_identity_from_payload', 'require_single_source_identity', 'queue_v3_source_links'}


def test_queue_sources_executor_has_no_source_contract_definitions():
    tree = queue_engine_tree()
    names = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert not names.intersection(_queue_sources_NAMES)


_queue_task_records_SOURCE = ROOT / 'scripts/llm_wiki/queue_task_records.py'


def test_queue_task_records_required_queue_calls_use_aliases_not_duplicate_implementations():
    tree = queue_engine_tree()
    names = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
    local = ast.parse(_queue_task_records_SOURCE.read_text(encoding='utf-8'))
    owned = {node.name for node in local.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
    assert not names & owned
    assert '_read_exported_rows' in names




def test_queue_timing_executor_does_not_define_timing_functions():
    tree = queue_engine_tree()
    names = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert not names.intersection(timing_function_names())


def _queue_validation_defined_functions(path):
    return {n.name for n in ast.parse(path.read_text(encoding='utf-8')).body if isinstance(n, ast.FunctionDef)}


def test_queue_validation_executor_no_longer_defines_owned_function():
    owned = _queue_validation_defined_functions(ROOT / 'scripts/llm_wiki/queue_validation.py')
    executor = {n.name for n in queue_engine_tree().body if isinstance(n, ast.FunctionDef)}
    assert not owned.intersection(executor)


def test_queue_worker_state_queue_has_no_second_worker_value_implementation():
    tree = queue_engine_tree()
    names = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    assert not names.intersection({'QueueRetryPolicy', 'ProcessorOutcome', 'WorkerProgress', 'processor_error_code'})


def test_queue_worker_state_capture_reason_conversion_does_not_import_queue_executor():
    tree = ast.parse((ROOT / 'scripts/flush_memory.py').read_text(encoding='utf-8'))
    function = next(n for n in tree.body if getattr(n, 'name', None) == 'capture_queue_failure')
    assert isinstance(function, ast.FunctionDef)
    imports = {n.module for n in ast.walk(function) if isinstance(n, ast.ImportFrom)}
    assert 'memory_queue' not in imports
    assert 'llm_wiki.queue_worker_state' in imports


_queue_contracts_NAMES = frozenset(json.loads(((ROOT / 'tests/modules/queue_contracts/test_boundary.py').parent / 'fixtures/legacy-pickle.json').read_text(encoding='utf-8'))['names'])


_QUEUE_EXECUTOR_MODULES = frozenset({'memory_queue', 'queue_core'})


def _queue_contracts_legacy_import(node):
    if not isinstance(node, ast.ImportFrom) or node.module not in _QUEUE_EXECUTOR_MODULES:
        return False
    return any(alias.name in _queue_contracts_NAMES or alias.name == '*' for alias in node.names)


def _queue_contracts_module_aliases(node):
    if not isinstance(node, ast.Import):
        return set()
    return {alias.asname or alias.name for alias in node.names if alias.name in _QUEUE_EXECUTOR_MODULES}


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


@pytest.mark.parametrize('source', ['from memory_queue import QueueFailure', 'from memory_queue import QueueFailure as Failure', 'from memory_queue import *', 'import memory_queue; failure = memory_queue.QueueFailure', 'import memory_queue as queue; failure = queue.QueueFailure', 'def local():\n    from memory_queue import QueueFailure', 'from queue_core import QueueFailure', 'import queue_core as queue; failure = queue.QueueFailure'])
def test_queue_contracts_guard_detects_executor_dependency(source):
    assert _queue_contracts_violations(source)


@pytest.mark.parametrize('source', ['from memory_queue import MemoryQueue', 'import memory_queue as queue; queue.active_or_legacy_memory_queue()', 'from llm_wiki.queue_contracts import QueueFailure'])
def test_queue_contracts_guard_allows_operations_and_owned_types(source):
    assert not _queue_contracts_violations(source)


def test_queue_resolution_uses_the_call_lifecycle_owner():
    paths = [*queue_engine_files(), SCRIPTS / 'memory_queue.py']
    sources = [path.read_text(encoding='utf-8') for path in paths]
    imports = [node for source in sources for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ImportFrom)]
    resolvers = _call_resolver_imports(imports)
    assert resolvers == ['llm_wiki.compile_call_runtime', 'llm_wiki.compile_call_runtime']
    assert 'compile_controller' not in _imported_module_names(imports)


def _imported_module_names(imports):
    return {node.module for node in imports}


def _call_resolver_imports(imports):
    return [node.module for node in imports
            if any(a.name == 'resolve_model_call' for a in node.names)]


def test_the_queue_source_alone_stays_within_the_module_limit():
    # Basis: Law 11; a module and its tests may not pass 100000 o200k_base tokens.
    source = (ROOT / 'scripts/queue_core.py').read_text(encoding='utf-8')
    assert count_tokens(source) <= 100_000


def _queue_facade_tree():
    return ast.parse((SCRIPTS / 'queue_core.py').read_text(encoding='utf-8'))


def _queue_imported_aliases():
    imports = (node for node in ast.walk(_queue_facade_tree()) if isinstance(node, ast.ImportFrom))
    return [alias for node in imports for alias in node.names]


def _queue_self_aliases():
    return {alias.name for alias in _queue_imported_aliases() if alias.asname == alias.name}


def _names_read_inside_the_queue_facade():
    return {node.id for node in ast.walk(_queue_facade_tree()) if isinstance(node, ast.Name)}


def _python_sources_beside_the_queue_facade():
    roots = ('scripts', 'tests', 'benchmark')
    paths = (path for root in roots for path in (ROOT / root).rglob('*.py'))
    return [path.read_text(encoding='utf-8') for path in paths if path != SCRIPTS / 'queue_core.py']


def _parsed(text):
    try:
        return ast.parse(text)
    except SyntaxError:
        return ast.Module(body=[], type_ignores=[])


def _imported_aliases(tree, kind):
    return [alias for node in ast.walk(tree) if isinstance(node, kind) for alias in _facade_aliases(node)]


def _facade_aliases(node):
    if isinstance(node, ast.ImportFrom):
        return node.names if node.module == 'queue_core' else []
    return [alias for alias in node.names if alias.name == 'queue_core']


def _names_imported_from_the_queue_facade(tree):
    return {alias.name for alias in _imported_aliases(tree, ast.ImportFrom)}


def _local_names_of_the_queue_facade(tree):
    return {alias.asname or alias.name for alias in _imported_aliases(tree, ast.Import)}


def _attributes_read_from(local_names, text):
    return {name for local in local_names for name in re.findall(rf'\b{local}\.(\w+)', text)}


def _names_read_through_the_queue_facade(sources):
    read = set()
    for text in sources:
        tree = _parsed(text)
        read |= _attributes_read_from({'queue_core'} | _local_names_of_the_queue_facade(tree), text)
        read |= set(re.findall(r'''['"]queue_core['"]\s*,\s*['"](\w+)''', text))
        read |= _names_imported_from_the_queue_facade(tree)
    return read


def test_every_queue_self_alias_has_a_consumer():
    aliases = _queue_self_aliases()
    used_inside = _names_read_inside_the_queue_facade()
    used_elsewhere = _names_read_through_the_queue_facade(_python_sources_beside_the_queue_facade())
    assert sorted(aliases - used_inside - used_elsewhere) == []


def test_a_name_merely_mentioned_elsewhere_is_not_a_facade_consumer():
    mention = 'from llm_wiki.queue_schema import QUEUE_V3_TABLE_SQL\nQUEUE_V3_TABLE_SQL\n'
    reads = 'import queue_core\nqueue_core.MemoryQueue\nfrom queue_core import run_worker\n'
    renamed = 'import queue_core as queue_module\nqueue_module.settle_child_exit(run)\n'
    assert _names_read_through_the_queue_facade([mention]) == set()
    assert _names_read_through_the_queue_facade([reads]) == {'MemoryQueue', 'run_worker'}
    assert _names_read_through_the_queue_facade([renamed]) == {'settle_child_exit'}


def _protocol_methods(protocol):
    return [name for name, value in vars(protocol).items() if callable(value) and not name.startswith('_')]


def test_the_queue_offers_every_operation_the_worker_calls_with_the_same_parameters():
    import inspect

    import queue_core
    from llm_wiki.queue_worker import WorkerQueue

    for name in _protocol_methods(WorkerQueue):
        expected = list(inspect.signature(getattr(WorkerQueue, name)).parameters)
        assert list(inspect.signature(getattr(queue_core.MemoryQueue, name)).parameters) == expected
