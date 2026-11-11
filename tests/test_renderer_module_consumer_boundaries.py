"""Consumer integration assertions retained from leaf module qualification."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def test_utf8_text_renderer_has_no_second_prefix_implementation():
    tree = ast.parse((ROOT / 'scripts/code_navigation_renderer.py').read_text(encoding='utf-8'))
    names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert 'utf8_prefix' not in names
