"""Saved answer validation cannot import the executable controller."""
import ast
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports

ROOT = Path(__file__).resolve().parents[3]


def test_answer_records_use_only_lower_value_and_journal_contracts():
    source = ROOT / 'scripts/llm_wiki/compile_answer_records.py'
    assert nonstdlib_imports(source.read_text()) == {
        'compile_progress', 'context_budget', 'llm_wiki.model_records',
        'llm_wiki.formats.canonical',
    }


def test_import_has_no_queue_controller_or_provider_transport():
    assert_isolated_import(ROOT, 'llm_wiki.compile_answer_records', dependencies=(
        'llm_wiki', 'compile_progress', 'context_budget', 'llm_wiki.model_records',
        'llm_wiki.formats', 'llm_wiki.formats.canonical', 'llm_wiki.sqlite_deadlines',
    ))


def _named_functions(tree, name):
    return [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name]


def test_retained_queue_answer_is_verified_without_importing_the_controller():
    tree = ast.parse((ROOT / 'scripts/memory_queue.py').read_text())
    functions = _named_functions(tree, '_verify_retained_compile_answer')
    assert len(functions) == 1
    imports = {n.module for n in ast.walk(functions[0]) if isinstance(n, ast.ImportFrom)}
    assert imports == {'llm_wiki.compile_answer_records'}
