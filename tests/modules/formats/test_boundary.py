"""Formats must be usable without loading an execution component."""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

from tests.module_context import context_reader
from tests.slow_machine import LONG_TIMEOUT

ROOT = Path(__file__).resolve().parents[3]


read_source = context_reader(ROOT, ROOT / 'docs/modules/formats-context.json')
SCRIPTS = ROOT / "scripts"


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
from llm_wiki.formats import canonical, schema, source, receipts, queue_payload
assert queue_payload.validate_payload_blob(b'{}', canonical.sha256_bytes(b'{}'), parse=True).payload == {}
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
        tree = ast.parse(read_source(path, encoding='utf-8'))
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
