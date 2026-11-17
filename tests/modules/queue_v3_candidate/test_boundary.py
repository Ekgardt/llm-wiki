"""Queue v3 candidate depends only on what it declares."""
import ast
from pathlib import Path

from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
DECLARED = ['__future__', 'llm_wiki.formats.canonical', 'llm_wiki.formats.queue_payload', 'llm_wiki.sqlite_contracts', 're', 'sqlite3']

read_source = context_reader(ROOT, ROOT / 'docs/modules/queue-v3-candidate-context.json')


def _from_imports(tree):
    return {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}


def _plain_imports(tree):
    statements = [node for node in ast.walk(tree) if isinstance(node, ast.Import)]
    return {alias.name for statement in statements for alias in statement.names}


def test_owner_imports_exactly_its_declared_dependencies():
    tree = ast.parse(read_source(ROOT / 'scripts/llm_wiki/queue_v3_candidate.py'))
    assert _from_imports(tree) | _plain_imports(tree) == set(DECLARED)
