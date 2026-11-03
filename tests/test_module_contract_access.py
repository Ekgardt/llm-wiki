"""Unsupported dependency accesses must keep full implementation context."""
import pytest

from tests.module_contract_access import require_direct_public_access

EXPORTS = {'estimate': 'function', 'RATIO': 'constant'}
IMPORT = 'from provider import estimate, RATIO\n'


def _check(source):
    return require_direct_public_access(source, 'provider', EXPORTS)


def test_direct_calls_and_immutable_constant_reads_are_supported():
    assert _check(IMPORT + 'result = estimate({"ratio": RATIO})') == {
        'estimate': 'estimate', 'RATIO': 'RATIO',
    }


def test_explicit_function_alias_is_checked_as_the_same_public_member():
    assert _check('from provider import estimate as count\nvalue = count(1)') == {
        'count': 'estimate',
    }


@pytest.mark.parametrize('source', [
    'from provider import _private',
    'from provider import other',
    'from provider import *',
    'import provider',
    'import provider as p',
    'import scripts.provider as p',
    'from scripts import provider as p',
    'from scripts.provider import estimate',
    'from .provider import estimate',
    'def work():\n    from provider import estimate',
    'if enabled:\n    from provider import estimate',
    'from provider import estimate\nfrom provider import estimate',
])
def test_unsupported_import_shape_is_refused(source):
    with pytest.raises(ValueError):
        _check(source)


@pytest.mark.parametrize('use', [
    'other = estimate', 'accept(estimate)', 'saved = [estimate]',
    'value = estimate.__globals__', 'estimate = replacement',
    'import other as estimate', 'from other import estimate',
    'def estimate():\n    pass', 'def work(estimate):\n    pass',
    'try:\n    pass\nexcept Exception as estimate:\n    pass',
    'def work():\n    global estimate',
    'match value:\n    case estimate:\n        pass',
])
def test_function_escape_reflection_and_shadowing_refuse(use):
    with pytest.raises(ValueError):
        _check(IMPORT + use)


@pytest.mark.parametrize('source', [
    'import importlib', 'from importlib import import_module as load',
    'import sys', 'from builtins import getattr as lookup',
    'lookup = getattr', 'value = globals()["estimate"]',
    'exec(source)', 'value = object.__getattribute__(obj, name)',
    'value = helper.getattr(obj, name)',
    'loader = __builtins__["__import__"]',
])
def test_dynamic_access_is_not_misrepresented_as_a_static_binding(source):
    with pytest.raises(ValueError):
        _check(IMPORT + source)
