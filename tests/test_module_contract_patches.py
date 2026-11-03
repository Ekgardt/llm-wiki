"""Only verified public fault injection is exempt from reflection rejection."""
import pytest

from tests.module_contract_patches import require_test_public_calls

INTERFACE = 'RATIO = 2\ndef estimate(data):\n    ...\n'
CONSUMER = 'from provider import estimate, RATIO\ndef cost(data):\n    return estimate(data)\n'
TEST = '''import consumer
def test_unknown(monkeypatch):
    monkeypatch.setattr(consumer, "estimate", lambda data: None)
    assert consumer.cost({}) is None
'''


def _check(source):
    return require_test_public_calls(source, module='provider', interface=INTERFACE,
                                     consumer_module='consumer', consumer_source=CONSUMER)


def test_public_fault_injection_does_not_require_private_implementation():
    assert _check(TEST) == {}


@pytest.mark.parametrize(('before', 'after'), [
    ('"estimate"', '"_private"'), ('"estimate"', '"unknown"'),
    ('"estimate"', '"RATIO"'), ('"estimate"', 'member'),
    ('setattr(consumer,', 'setattr(provider,'),
    ('monkeypatch.setattr', 'other.setattr'),
    ('def test_unknown(monkeypatch):', 'def test_unknown():'),
    ('def test_unknown', 'def helper'),
    ('lambda data: None', 'lambda data: globals()'),
    ('lambda data: None)', 'lambda data: None, raising=False)'),
])
def test_implementation_sensitive_or_unverifiable_patch_refuses(before, after):
    with pytest.raises(ValueError):
        _check(TEST.replace(before, after))


@pytest.mark.parametrize('statement', [
    'consumer = other', 'monkeypatch = other', 'import other as consumer',
    'from other import monkeypatch', 'def consumer():\n    pass',
    'def monkeypatch():\n    pass', 'def helper(consumer):\n    pass',
])
def test_patch_target_and_fixture_cannot_be_rebound_or_shadowed(statement):
    with pytest.raises(ValueError):
        _check(TEST + '\n' + statement)


def test_replacement_expression_keeps_full_dynamic_access_checks():
    altered = TEST.replace('lambda data: None', 'lambda data: __builtins__["eval"](data)')
    with pytest.raises(ValueError, match='Dynamic'):
        _check(altered)


def test_direct_provider_imports_in_the_same_test_keep_call_shape_checks():
    altered = TEST + '\nfrom provider import estimate\nvalue = estimate()\n'
    with pytest.raises(ValueError, match='data'):
        _check(altered)


@pytest.mark.parametrize('use', ['value = consumer.estimate()', 'from consumer import estimate'])
def test_indirect_reexports_are_not_unchecked_dependency_calls(use):
    with pytest.raises(ValueError, match='reexports'):
        _check(TEST + '\n' + use)


def test_local_alias_of_consumer_module_remains_an_explicit_binding():
    assert _check(TEST.replace('import consumer', 'import consumer as c').replace(
        'setattr(consumer,', 'setattr(c,').replace('consumer.cost', 'c.cost')) == {}
