"""SQL definitions have one owner, without runtime imports."""
import ast
from pathlib import Path

from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]


read_source = context_reader(ROOT, ROOT / 'docs/modules/queue-schema-context.json')


def test_schema_has_no_imports():
    tree = ast.parse(read_source(ROOT / 'scripts/llm_wiki/queue_schema.py'))
    assert not any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in ast.walk(tree))
