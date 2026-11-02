"""Heartbeat implementation has one independent owner."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_executor_does_not_define_heartbeat_implementation():
    tree = ast.parse((ROOT / 'scripts/memory_queue.py').read_text())
    names = {node.name for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef))}
    assert not names.intersection({'_LeaseHeartbeat', '_SourceFenceHeartbeat', '_join_heartbeat_or_refuse'})


def test_owner_imports_only_declared_dependencies():
    tree = ast.parse((ROOT / 'scripts/llm_wiki/queue_heartbeat.py').read_text())
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert imports <= {'__future__', 'typing', 'lease_renewal', 'llm_wiki.queue_contracts',
                       'llm_wiki.reliability_defaults'}


def test_direct_imports_are_stdlib():
    tree = ast.parse((ROOT / 'scripts/llm_wiki/queue_heartbeat.py').read_text())
    direct = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    assert direct <= {'threading', 'time'}
