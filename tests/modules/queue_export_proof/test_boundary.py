"""Queue export proof depends only on what it declares."""
import ast
from pathlib import Path

from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
DECLARED = ['__future__', 'collections.abc', 'json', 'llm_wiki.formats.canonical', 'llm_wiki.formats.queue_payload', 'llm_wiki.formats.schema', 'llm_wiki.queue_artifacts', 'llm_wiki.queue_contracts', 'llm_wiki.queue_evidence_records', 'pathlib', 'sqlite3', 'stat']

read_source = context_reader(ROOT, ROOT / 'docs/modules/queue-export-proof-context.json')


def _from_imports(tree):
    return {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}


def _plain_imports(tree):
    statements = [node for node in ast.walk(tree) if isinstance(node, ast.Import)]
    return {alias.name for statement in statements for alias in statement.names}


def test_owner_imports_exactly_its_declared_dependencies():
    tree = ast.parse(read_source(ROOT / 'scripts/llm_wiki/queue_export_proof.py'))
    assert _from_imports(tree) | _plain_imports(tree) == set(DECLARED)
