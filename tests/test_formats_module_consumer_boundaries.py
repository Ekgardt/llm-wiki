"""Consumer integration assertions retained from leaf module qualification."""
import ast
from pathlib import Path

import pytest

from tests.script_sources import split_module_files

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


@pytest.mark.parametrize('consumer', [*split_module_files("doctor", "doctor"), SCRIPTS / 'evidence_resolver.py'], ids=lambda path: path.name)
def test_formats_receipt_readers_do_not_import_parsers_from_compiler(consumer):
    tree = ast.parse(consumer.read_text(encoding='utf-8'))
    bad = [node for node in ast.walk(tree) if _formats_imports_compiler_parser(node)]
    assert not bad, f'{consumer} obtains a format parser from its executor'


def _formats_imports_compiler_parser(node):
    if not isinstance(node, ast.ImportFrom):
        return False
    if node.module != 'compile_memory':
        return False
    return any(alias.name.startswith('parse_compile_receipt') for alias in node.names)


_formats_LEGACY_FORMAT_EXPORTS = {'memory_queue': frozenset({'PayloadValidation', 'validate_payload_blob'}), 'reliable_memory': frozenset({'canonical_json_bytes', 'sha256_bytes', 'validate_schema', 'validate_schema_object', 'SchemaValidationError'}), 'compile_cache': frozenset({'SourceDescriptor', 'SourceOccurrenceBounds'}), 'compile_memory': frozenset({'parse_compile_receipt_v2', 'parse_compile_receipt_v3', 'parse_compile_receipt_v4', 'parse_compile_receipt_version', 'UnsupportedCompileReceiptVersion', 'compile_source_identity', 'compile_context_source_identity', 'COMPILE_RECEIPT_SCHEMA', 'COMPILE_RECEIPT_V3_SCHEMA', 'COMPILE_RECEIPT_V4_SCHEMA'})}


def _formats_legacy_module_aliases(node):
    if not isinstance(node, ast.Import):
        return ()
    return [alias for alias in node.names if alias.name in _formats_LEGACY_FORMAT_EXPORTS]


def _formats_legacy_aliases(tree):
    aliases = {}
    for node in ast.walk(tree):
        for alias in _formats_legacy_module_aliases(node):
            aliases.setdefault(alias.asname or alias.name, set()).add(alias.name)
    return aliases


def _formats_qualified_legacy_access(node, aliases):
    if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
        return False
    return any(node.attr in _formats_LEGACY_FORMAT_EXPORTS[owner] for owner in aliases.get(node.value.id, ()))


def _formats_legacy_format_access(node, aliases):
    if isinstance(node, ast.ImportFrom):
        exports = _formats_LEGACY_FORMAT_EXPORTS.get(node.module, ())
        return any(alias.name in exports or (alias.name == '*' and exports) for alias in node.names)
    return _formats_qualified_legacy_access(node, aliases)


def _formats_legacy_format_lines(tree):
    aliases = _formats_legacy_aliases(tree)
    return [node.lineno for node in ast.walk(tree) if _formats_legacy_format_access(node, aliases)]


def test_formats_internal_consumers_use_the_formats_owner():
    offenders = {}
    for path in SCRIPTS.rglob('*.py'):
        lines = _formats_legacy_format_lines(ast.parse(path.read_text(encoding='utf-8')))
        if lines:
            offenders[str(path.relative_to(SCRIPTS))] = lines
    assert not offenders, offenders


@pytest.mark.parametrize('code', ['from reliable_memory import canonical_json_bytes as encode', 'from compile_cache import SourceDescriptor', 'from compile_memory import parse_compile_receipt_version', 'from reliable_memory import *', 'import reliable_memory as old\nold.canonical_json_bytes({})', 'def reader():\n import compile_memory as old\n return old.compile_source_identity("a", "b")'])
def test_formats_legacy_import_guard_sees_aliases_and_local_imports(code):
    assert _formats_legacy_format_lines(ast.parse(code))


@pytest.mark.parametrize('code', ['from reliable_memory import open_operational_db', 'import reliable_memory\nreliable_memory.open_operational_db("a")', 'from compile_memory import read_compile_receipt_version', 'from llm_wiki.formats.canonical import canonical_json_bytes'])
def test_formats_legacy_import_guard_keeps_execution_and_owner_imports(code):
    assert not _formats_legacy_format_lines(ast.parse(code))
