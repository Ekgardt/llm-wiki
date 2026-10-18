"""Correction spending is an explicit operator choice, never a hidden constant."""
import compile_memory as cm
import pytest
from settings import SettingsError

from tests.test_compile_failure import _attempt


@pytest.mark.parametrize('retries', [0, 1, 4])
def test_configured_number_of_corrections_reaches_the_provider_loop(monkeypatch, retries):
    monkeypatch.setenv('LLM_WIKI_COMPILE_VALIDATION_RETRIES', str(retries))
    attempt, calls = _attempt(monkeypatch, ['validation_error'] * (retries + 1))
    assert attempt.resolve(cm.provider_candidates('fake')[0]) is None
    assert len(calls) == retries + 1


def test_invalid_retry_configuration_fails_before_any_call(monkeypatch):
    monkeypatch.setenv('LLM_WIKI_COMPILE_VALIDATION_RETRIES', '-1')
    attempt, calls = _attempt(monkeypatch, ['validation_error'])
    with pytest.raises(SettingsError, match='validation_retries'):
        attempt.resolve(cm.provider_candidates('fake')[0])
    assert calls == []
