"""The supervisor stays importable without loading its worker or product runtime."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, assert_stdlib_boundary
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]

read_source = context_reader(ROOT, ROOT / 'docs/modules/mcp-supervisor-context.json')


def test_supervisor_has_only_standard_library_imports():
    assert_stdlib_boundary(ROOT, 'mcp_supervisor', read_source=read_source)


def test_supervisor_does_not_import_the_worker_or_queue():
    assert_isolated_import(ROOT, 'mcp_supervisor')
