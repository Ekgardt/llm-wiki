"""Formats must be usable without loading an execution component."""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from tests.slow_machine import LONG_TIMEOUT

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts"


@pytest.mark.parametrize("consumer", ["doctor.py", "evidence_resolver.py"])
def test_receipt_readers_do_not_import_parsers_from_compiler(consumer):
    tree = ast.parse((SCRIPTS / consumer).read_text(encoding="utf-8"))
    bad = [node for node in ast.walk(tree) if _imports_compiler_parser(node)]
    assert not bad, f"{consumer} obtains a format parser from its executor"


def _imports_compiler_parser(node):
    if not isinstance(node, ast.ImportFrom):
        return False
    if node.module != "compile_memory":
        return False
    return any(alias.name.startswith("parse_compile_receipt") for alias in node.names)


def test_formats_load_without_runtime_or_execution_imports(tmp_path):
    probe = '''
import importlib.abc
import sys
import os
import json
from pathlib import Path


def no_execution(event, args):
    if event.startswith("socket.") or event in {"sqlite3.connect", "subprocess.Popen",
            "os.mkdir", "os.remove", "os.rename", "os.rmdir", "os.chmod", "os.truncate"}:
        raise AssertionError("format attempted a side effect: " + event)
    if event == "open" and args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
        raise AssertionError("format attempted a write")

sys.addaudithook(no_execution)

class OnlyFormats(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {"compile_memory", "compile_cache", "memory_queue", "doctor",
                        "markdown_transaction", "reliable_memory", "bounded_io",
                        "llm_client", "memory_state", "settings"}:
            raise AssertionError("format imported execution code: " + fullname)

sys.meta_path.insert(0, OnlyFormats())
sys.path.insert(0, sys.argv[1])
from llm_wiki.formats import canonical, schema, source, receipts
assert canonical.canonical_json_bytes({"x": 1}) == b'{"x":1}'
schema.validate_schema_object({"x": 1}, {"type": "object"})
assert source.SourceDescriptor("knowledge/daily/a.md", 0, "a" * 64).canonical()
assert receipts.receipt_source_fields(b"invalid") is None
fixtures = Path(sys.argv[2])
for version in ("v3", "v4"):
    record = json.loads((fixtures / (version + ".json")).read_bytes())
    parsed = receipts.parse_compile_receipt_version(
        (fixtures / (version + ".md")).read_bytes(),
        logical_path=record["source"]["logical_path"], source_sha256=record["source"]["sha256"])
    assert parsed == record
record = json.loads((fixtures / "v2.json").read_bytes())
assert receipts.parse_compile_receipt_v2((fixtures / "v2.md").read_bytes(), record["source_digest"]) == record
'''
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", probe, str(SCRIPTS), str(Path(__file__).with_name("fixtures"))],
        cwd=tmp_path, capture_output=True, text=True, check=False, timeout=LONG_TIMEOUT,
    )
    assert result.returncode == 0, result.stderr
    assert list(tmp_path.iterdir()) == []


def test_all_format_imports_are_standard_library_or_the_same_module():
    for path in (SCRIPTS / 'llm_wiki/formats').glob('*.py'):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        modules = _absolute_imports(tree)
        assert all(node.level <= 1 for node in ast.walk(tree) if isinstance(node, ast.ImportFrom))
        assert modules <= sys.stdlib_module_names, (path, modules - sys.stdlib_module_names)


def _absolute_imports(tree):
    modules = set()
    for node in ast.walk(tree):
        modules.update(_import_roots(node))
    return modules


def _import_roots(node):
    if isinstance(node, ast.Import):
        return {alias.name.split('.')[0] for alias in node.names}
    if isinstance(node, ast.ImportFrom) and node.level == 0:
        return {node.module.split('.')[0]}
    return set()


_LEGACY_FORMAT_EXPORTS = {
    'reliable_memory': frozenset({
        'canonical_json_bytes', 'sha256_bytes', 'validate_schema',
        'validate_schema_object', 'SchemaValidationError',
    }),
    'compile_cache': frozenset({'SourceDescriptor', 'SourceOccurrenceBounds'}),
    'compile_memory': frozenset({
        'parse_compile_receipt_v2', 'parse_compile_receipt_v3',
        'parse_compile_receipt_v4', 'parse_compile_receipt_version',
        'UnsupportedCompileReceiptVersion', 'compile_source_identity',
        'compile_context_source_identity', 'COMPILE_RECEIPT_SCHEMA',
        'COMPILE_RECEIPT_V3_SCHEMA', 'COMPILE_RECEIPT_V4_SCHEMA',
    }),
}


def _legacy_module_aliases(node):
    if not isinstance(node, ast.Import):
        return ()
    return [alias for alias in node.names if alias.name in _LEGACY_FORMAT_EXPORTS]


def _legacy_aliases(tree):
    aliases = {}
    for node in ast.walk(tree):
        for alias in _legacy_module_aliases(node):
            aliases.setdefault(alias.asname or alias.name, set()).add(alias.name)
    return aliases


def _qualified_legacy_access(node, aliases):
    if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
        return False
    return any(node.attr in _LEGACY_FORMAT_EXPORTS[owner]
               for owner in aliases.get(node.value.id, ()))


def _legacy_format_access(node, aliases):
    if isinstance(node, ast.ImportFrom):
        exports = _LEGACY_FORMAT_EXPORTS.get(node.module, ())
        return any(alias.name in exports or (alias.name == '*' and exports) for alias in node.names)
    return _qualified_legacy_access(node, aliases)


def _legacy_format_lines(tree):
    aliases = _legacy_aliases(tree)
    return [node.lineno for node in ast.walk(tree) if _legacy_format_access(node, aliases)]


def test_internal_consumers_use_the_formats_owner():
    offenders = {}
    for path in SCRIPTS.rglob('*.py'):
        lines = _legacy_format_lines(ast.parse(path.read_text(encoding='utf-8')))
        if lines:
            offenders[str(path.relative_to(SCRIPTS))] = lines
    assert not offenders, offenders


@pytest.mark.parametrize('code', [
    'from reliable_memory import canonical_json_bytes as encode',
    'from compile_cache import SourceDescriptor',
    'from compile_memory import parse_compile_receipt_version',
    'from reliable_memory import *',
    'import reliable_memory as old\nold.canonical_json_bytes({})',
    'def reader():\n import compile_memory as old\n return old.compile_source_identity("a", "b")',
])
def test_legacy_import_guard_sees_aliases_and_local_imports(code):
    assert _legacy_format_lines(ast.parse(code))


@pytest.mark.parametrize('code', [
    'from reliable_memory import open_operational_db',
    'import reliable_memory\nreliable_memory.open_operational_db("a")',
    'from compile_memory import read_compile_receipt_version',
    'from llm_wiki.formats.canonical import canonical_json_bytes',
])
def test_legacy_import_guard_keeps_execution_and_owner_imports(code):
    assert not _legacy_format_lines(ast.parse(code))
