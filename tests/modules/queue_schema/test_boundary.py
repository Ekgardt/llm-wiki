"""SQL definitions have one owner, without runtime imports."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
NAMES = {'_QUEUE_V3_TABLE_SQL', '_QUEUE_V3_INDEX_SQL', '_QUEUE_V3_TRIGGER_SQL'}


def test_executor_does_not_define_schema():
    tree = ast.parse((ROOT / 'scripts/memory_queue.py').read_text())
    assigned = {node.id for node in ast.walk(tree)
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)}
    assert not assigned.intersection(NAMES)


def test_schema_has_no_imports():
    tree = ast.parse((ROOT / 'scripts/llm_wiki/queue_schema.py').read_text())
    assert not any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in ast.walk(tree))
