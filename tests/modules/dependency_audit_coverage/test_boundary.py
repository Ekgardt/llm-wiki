"""Audit verification has no product-executor or advisory-client dependency."""
import sys
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/dependency-audit-coverage-context.json')


def test_only_python310_toml_fallback_is_external():
    source = read_source(ROOT / 'scripts/check_dependency_audit.py')
    assert nonstdlib_imports(source) == {'tomli', 'tomllib'} - sys.stdlib_module_names


def test_import_does_not_start_product_or_advisory_client():
    assert_isolated_import(ROOT, 'check_dependency_audit', dependencies=('tomli',))
