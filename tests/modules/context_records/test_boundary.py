"""Record maintenance stays outside product executors and runtime state."""
from pathlib import Path

import pytest

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize('name', ['module_records', 'module_record_context', 'module_record_ownership'])
def test_record_tools_import_without_product_executors(name):
    assert_isolated_import(ROOT, 'tests.' + name)


def test_partition_validator_depends_only_on_standard_library():
    assert not nonstdlib_imports((ROOT / 'tests/module_records.py').read_text())
