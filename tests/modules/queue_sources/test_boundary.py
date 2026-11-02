"""The pure source contract must not be owned by the queue executor."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
NAMES = {'_collect_identity_from_mapping', '_collect_source_identity_strings',
         '_source_identity_strings', '_distinct_source_identities',
         '_payload_identity_fields', '_identity_paths_or_daily',
         '_validated_failure_identity', '_require_canonical_daily_id',
         '_unsafe_link_shape', '_unsafe_link_path', '_link_pair',
         '_normalized_source_link', '_normalized_source_links',
         '_validate_source_identity', '_validate_failure_identity',
         '_source_identity_from_payload'}


def test_executor_has_no_source_contract_definitions():
    tree = ast.parse((ROOT / 'scripts/memory_queue.py').read_text())
    names = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert not names.intersection(NAMES)


def test_source_contract_imports_only_stdlib():
    tree = ast.parse((ROOT / 'scripts/llm_wiki/queue_sources.py').read_text())
    imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert imported <= {'__future__', 'datetime', 'pathlib', 'types'}


def test_direct_imports_are_stdlib():
    tree = ast.parse((ROOT / 'scripts/llm_wiki/queue_sources.py').read_text())
    direct = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
              for alias in node.names}
    assert direct <= {'json', 're'}
