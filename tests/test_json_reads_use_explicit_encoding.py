"""Repository integration: JSON text reads must not inherit the host code page."""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _implicit_encoding(call):
    return not call.args and all(item.arg != 'encoding' for item in call.keywords)


def _implicit_text_read(node):
    if not isinstance(node, ast.Call):
        return False
    return getattr(node.func, 'attr', None) == 'read_text' and _implicit_encoding(node)


def _json_text_reads(node):
    if not isinstance(node, ast.Call):
        return []
    if getattr(node.func, 'attr', None) != 'loads':
        return []
    return [child.lineno for child in ast.walk(node) if _implicit_text_read(child)]


def _default_json_reads(source):
    return [line for node in ast.walk(ast.parse(source)) for line in _json_text_reads(node)]


@pytest.mark.parametrize('source', [
    'json.loads(path.read_text())', 'j.loads((root / "fixture.json").read_text())',
])
def test_guard_detects_original_code_page_dependency(source):
    assert _default_json_reads(source)


@pytest.mark.parametrize('source', [
    'json.loads(path.read_text(encoding="utf-8"))',
    'json.loads(path.read_text("utf-8"))', 'json.loads(path.read_bytes())',
])
def test_guard_accepts_explicit_decoding(source):
    assert not _default_json_reads(source)


def _repository_sources():
    return sorted((ROOT / 'scripts').rglob('*.py')) + sorted((ROOT / 'tests').rglob('test_*.py'))


def test_repository_json_reads_declare_their_encoding():
    findings = {str(path.relative_to(ROOT)): _default_json_reads(path.read_bytes())
                for path in _repository_sources()}
    assert not {name: lines for name, lines in findings.items() if lines}


def _module_inspection_tests():
    tests = ROOT / 'tests'
    return set(tests.glob('*module*.py')) | set(tests.glob('*compatibility*.py')) | set((tests / 'modules').rglob('*.py'))


def _default_text_reads(source):
    return [node.lineno for node in ast.walk(ast.parse(source)) if _implicit_text_read(node)]


def test_module_source_inspection_declares_text_encoding():
    findings = {str(path.relative_to(ROOT)): _default_text_reads(path.read_bytes())
                for path in _module_inspection_tests()}
    assert not {name: lines for name, lines in findings.items() if lines}
