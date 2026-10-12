"""Sizing reuses source parsing; final validation still checks current authority."""
import compile_memory as compiler
import evidence_resolver
import fact_keys
import pytest
from compile_cache import CompileCache
from llm_client import ProviderDescriptor

from tests.test_compile_critic_preserves_native_claim_origin import _native_operation


def _attempt(root, inputs):
    return compiler._CompileAttempt(inputs, CompileCache(root / 'runtime'), None, None)


def _observe_parse(module, name, monkeypatch):
    calls = []
    original = getattr(module, name)

    def observed(content):
        calls.append(content)
        return original(content)

    monkeypatch.setattr(module, name, observed)
    return calls


def _descriptor():
    return ProviderDescriptor('fake', None, {}, {}, 0, ())


def test_native_critic_sizing_parses_each_immutable_source_once(tmp_path, monkeypatch):
    inputs, operation = _native_operation(tmp_path, monkeypatch)
    expected = compiler._critique_prompt(inputs, [operation])
    journals = _observe_parse(fact_keys, '_native_journal_index', monkeypatch)
    entries = _observe_parse(evidence_resolver, 'daily_entries', monkeypatch)
    attempt = _attempt(tmp_path, inputs)

    assert attempt._batch_fits(_descriptor(), [operation])
    assert attempt._batch_fits(_descriptor(), [operation])
    assert attempt._batch_fits(_descriptor(), [operation])
    assert len(journals) == 1
    assert len(entries) == 1
    assert compiler._SOURCE_CHOICE_PARSING.get() is None
    assert compiler._SOURCE_CHOICE_RESOLVER.get() is None

    assert compiler._critique_prompt(inputs, [operation]) == expected
    assert len(journals) > 1
    assert len(entries) > 1


def test_sizing_never_reuses_permanent_native_head_authority(tmp_path, monkeypatch):
    inputs, operation = _native_operation(tmp_path, monkeypatch)
    attempt = _attempt(tmp_path, inputs)
    assert attempt._batch_fits(_descriptor(), [operation])
    head = next(tmp_path.glob('knowledge/raw/sessions/**/*.breadcrumb.md'))
    head.write_bytes(head.read_bytes() + b'changed\n')
    with pytest.raises(ValueError):
        attempt._batch_fits(_descriptor(), [operation])


def test_separate_attempts_do_not_share_source_parsing(tmp_path, monkeypatch):
    inputs, operation = _native_operation(tmp_path, monkeypatch)
    journals = _observe_parse(fact_keys, '_native_journal_index', monkeypatch)
    first = _attempt(tmp_path, inputs)
    second = _attempt(tmp_path, inputs)
    assert first._batch_fits(_descriptor(), [operation])
    assert second._batch_fits(_descriptor(), [operation])
    assert len(journals) == 2


def test_warmed_sizing_cache_cannot_authorize_a_partial_quote(tmp_path, monkeypatch):
    inputs, operation = _native_operation(tmp_path, monkeypatch)
    attempt = _attempt(tmp_path, inputs)
    assert attempt._batch_fits(_descriptor(), [operation])
    operation['evidence'][0]['quoted_text'] = 'bicycle is red.'
    with pytest.raises(ValueError):
        attempt._batch_fits(_descriptor(), [operation])
    assert compiler._SOURCE_CHOICE_PARSING.get() is None
    assert compiler._SOURCE_CHOICE_RESOLVER.get() is None
