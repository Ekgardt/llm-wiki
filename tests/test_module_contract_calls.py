"""Shape errors and unknown argument expansion cannot qualify a call site."""
import pytest

from tests.module_contract_calls import require_public_calls

INTERFACE = 'def estimate(value, /, optional=None, *, scale=2):\n    ...\n'
IMPORT = 'from provider import estimate\n'


@pytest.mark.parametrize('call', ['estimate(1)', 'estimate(1, 2)',
                                'estimate(1, optional=2, scale=3)'])
def test_valid_public_shapes_bind(call):
    assert require_public_calls(IMPORT + call, 'provider', INTERFACE) == {'estimate': 'estimate'}


@pytest.mark.parametrize('call', [
    'estimate()', 'estimate(value=1)', 'estimate(1, 2, 3)', 'estimate(1, other=2)',
    'estimate(1, 2, optional=3)', 'estimate(*values)', 'estimate(1, **values)',
    'estimate(1, scale=2, scale=3)',
])
def test_wrong_and_dynamic_shapes_refuse(call):
    with pytest.raises(ValueError):
        require_public_calls(IMPORT + call, 'provider', INTERFACE)


def test_required_keyword_is_not_mistaken_for_default_none():
    interface = 'def estimate(*, required, optional=None):\n    ...\n'
    with pytest.raises(ValueError, match='required'):
        require_public_calls(IMPORT + 'estimate()', 'provider', interface)
    assert require_public_calls(IMPORT + 'estimate(required=1)', 'provider', interface)


def test_explicit_variadic_signature_accepts_statically_known_arguments():
    interface = 'def estimate(*values, **named):\n    ...\n'
    assert require_public_calls(IMPORT + 'estimate(1, 2, a=3)', 'provider', interface)


def test_alias_call_is_checked_against_the_original_declaration():
    source = 'from provider import estimate as count\ncount()'
    with pytest.raises(ValueError, match='value'):
        require_public_calls(source, 'provider', INTERFACE)


def test_constant_cannot_be_used_as_a_callable_export():
    with pytest.raises(ValueError, match='not a declared callable'):
        require_public_calls('from provider import RATIO\nRATIO(1)', 'provider', 'RATIO = 2')
