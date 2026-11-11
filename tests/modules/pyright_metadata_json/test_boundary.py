"""Metadata parsing does not import discovery or process ownership."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, assert_stdlib_boundary
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/pyright-metadata-json-context.json')


def test_parser_is_stdlib_only():
    assert_stdlib_boundary(ROOT, 'llm_wiki/pyright_metadata_json', read_source=read_source)


def test_import_excludes_discovery():
    assert_isolated_import(ROOT, 'llm_wiki.pyright_metadata_json', dependencies=('llm_wiki',))
