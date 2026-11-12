"""The timing functions as the owner's source defines them, for consumer guards."""
import ast
from pathlib import Path

OWNER = Path(__file__).resolve().parents[3] / 'scripts/llm_wiki/queue_timing.py'


def timing_function_names() -> set[str]:
    tree = ast.parse(OWNER.read_text(encoding='utf-8'))
    return {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
