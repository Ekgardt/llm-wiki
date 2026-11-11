"""Naming policy must not import its runtime consumers."""
from pathlib import Path

import pytest

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/vault-editorial-context.json')


@pytest.mark.parametrize('module', ['vault_editorial', 'vault_log'])
def test_policy_imports_only_the_standard_library(module):
    assert not nonstdlib_imports(read_source(ROOT / 'scripts' / f'{module}.py'))
    assert_isolated_import(ROOT, module)
