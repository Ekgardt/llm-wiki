"""Exception identity and evidence survive real cleanup failures."""
import traceback

import pytest
from interruption import (
    exception_reaches,
    interruption_in_chain,
    raise_collected_errors,
    walk_exception_chain,
)


def test_empty_chain_and_empty_cleanup_are_valid():
    assert list(walk_exception_chain(None)) == []
    assert not exception_reaches(None, RuntimeError())
    assert raise_collected_errors([]) is None


def test_chain_walk_visits_shared_cycles_only_once():
    first, second, third = (RuntimeError(name) for name in ('one', 'two', 'three'))
    first.__cause__ = second
    first.__context__ = third
    second.__cause__ = third
    third.__context__ = first
    visited = list(walk_exception_chain(first))
    assert len(visited) == 3
    assert {id(error) for error in visited} == {id(first), id(second), id(third)}
    assert exception_reaches(first, third)
    assert not exception_reaches(first, RuntimeError('three'))
    assert interruption_in_chain(first) is None


@pytest.mark.parametrize('kind', [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize('link', ['__cause__', '__context__'])
def test_wrapped_interruption_is_found_by_identity(kind, link):
    signal = kind('stop')
    wrapper = RuntimeError('wrapper')
    setattr(wrapper, link, signal)
    assert interruption_in_chain(wrapper) is signal
    assert exception_reaches(wrapper, signal)


def test_ordinary_cleanup_retains_first_error_and_prior_cause():
    prior, first, second = (RuntimeError(name) for name in ('prior', 'first', 'second'))
    with pytest.raises(RuntimeError) as caught:
        raise_collected_errors([first, second], prior_error=prior)
    assert caught.value is first
    assert caught.value.__cause__ is prior


def test_prior_error_is_raised_when_cleanup_itself_succeeds():
    prior = ValueError('operation failed')
    with pytest.raises(ValueError) as caught:
        raise_collected_errors([], prior_error=prior)
    assert caught.value is prior


@pytest.mark.parametrize('kind', [KeyboardInterrupt, SystemExit])
def test_later_interruption_outranks_ordinary_cleanup_failure(kind):
    ordinary, signal = RuntimeError('cleanup failed'), kind('stop')
    with pytest.raises(kind) as caught:
        raise_collected_errors([ordinary, signal])
    assert caught.value is signal
    assert signal.__cause__ is ordinary
    assert ordinary.__cause__ is None
    assert ordinary.__context__ is None


@pytest.mark.parametrize('kind', [KeyboardInterrupt, SystemExit])
def test_prior_interruption_precedes_collected_interruption(kind):
    prior, later = kind('first'), kind('second')
    with pytest.raises(kind) as caught:
        raise_collected_errors([later], prior_error=prior)
    assert caught.value is prior
    assert prior.__cause__ is later
    assert later.__cause__ is None


@pytest.mark.parametrize('kind', [KeyboardInterrupt, SystemExit])
def test_wrapped_interruption_retains_wrapper_without_back_edge(kind):
    signal = kind('stop')
    wrapper = RuntimeError('startup failed')
    wrapper.__cause__ = signal
    wrapper.__context__ = signal
    with pytest.raises(kind) as caught:
        raise_collected_errors([wrapper])
    assert caught.value is signal
    assert signal.__cause__ is wrapper
    assert wrapper.__cause__ is None
    assert wrapper.__context__ is None
    assert signal.__context__ is not signal


@pytest.mark.parametrize('kind', [KeyboardInterrupt, SystemExit])
def test_secondary_chain_is_detached_without_rewriting_deeper_evidence(kind):
    signal, middle, cleanup = kind('stop'), RuntimeError('middle'), RuntimeError('cleanup')
    middle.__cause__ = signal
    cleanup.__cause__ = middle
    with pytest.raises(kind) as caught:
        raise_collected_errors([signal, cleanup])
    assert caught.value is signal
    assert signal.__cause__ is cleanup
    assert cleanup.__cause__ is None
    assert middle.__cause__ is signal


def _original_interruption():
    try:
        raise KeyboardInterrupt('original frame')
    except KeyboardInterrupt as signal:
        return signal


def test_original_traceback_survives_cleanup_reraise():
    signal = _original_interruption()
    with pytest.raises(KeyboardInterrupt) as caught:
        raise_collected_errors([RuntimeError('cleanup'), signal])
    assert caught.value is signal
    assert '_original_interruption' in [frame.name for frame in traceback.extract_tb(signal.__traceback__)]


def test_active_exception_context_does_not_create_a_cycle():
    signal = KeyboardInterrupt('active')
    try:
        raise signal
    except KeyboardInterrupt:
        cleanup = RuntimeError('during cleanup')
        cleanup.__context__ = signal
        with pytest.raises(KeyboardInterrupt) as caught:
            raise_collected_errors([cleanup], prior_error=signal)
    assert caught.value is signal
    assert signal.__cause__ is cleanup
    assert cleanup.__context__ is None
    assert signal.__context__ is not signal
