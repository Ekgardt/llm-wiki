"""Reuse only an exact protected base within one disposable layout."""
from collections import Counter

import llm_client
from llm_wiki import compile_pipeline_operations, compile_pipeline_prompts

from tests.compile_binds_the_protected_source_view_helpers import _legacy_packet


def test_one_layout_scans_its_exact_base_once(tmp_path, monkeypatch):
    inputs = _legacy_packet(tmp_path, monkeypatch, ['A durable exact fact.'])
    base = compile_pipeline_operations.draft_base_prompt(inputs)
    calls = Counter()
    original = llm_client.protected_transport

    def observed(system, prompt, schema):
        calls[prompt] += 1
        return original(system, prompt, schema)

    monkeypatch.setattr(llm_client, 'protected_transport', observed)
    prompt, schema = compile_pipeline_prompts.draft_layout(inputs)
    assert calls[base] == 1
    assert calls[prompt] == 1
    assert schema
    compile_pipeline_prompts.draft_layout(inputs)
    assert calls[base] == 2


def test_layout_policy_drift_is_not_reused(tmp_path, monkeypatch):
    import dataclasses

    import model_dlp
    import pytest

    inputs = _legacy_packet(tmp_path, monkeypatch, ['A durable exact fact.'])
    original = model_dlp.load_policy
    policy = original()
    changed = dataclasses.replace(policy, literals=('durable',))
    policies = iter((policy, changed))

    def load():
        return next(policies)

    monkeypatch.setattr(model_dlp, 'load_policy', load)
    with pytest.raises(ValueError, match='identity changed'):
        compile_pipeline_prompts.draft_layout(inputs)


def test_appended_addresses_still_receive_full_scan(tmp_path, monkeypatch):
    import pytest

    from tests.compile_binds_the_protected_source_view_helpers import _policy

    inputs = _legacy_packet(tmp_path, monkeypatch, ['A durable exact fact.'])
    _policy(monkeypatch, tmp_path, literals=('LEGACY SOURCE ADDRESSES',))
    with pytest.raises(ValueError, match='changed protected source prefix'):
        compile_pipeline_prompts.draft_layout(inputs)


def test_public_prompt_keeps_fresh_unscoped_behavior(tmp_path, monkeypatch):
    inputs = _legacy_packet(tmp_path, monkeypatch, ['A durable exact fact.'])
    calls = Counter()
    original = llm_client.protected_transport

    def observed(system, prompt, schema):
        calls[prompt] += 1
        return original(system, prompt, schema)

    monkeypatch.setattr(llm_client, 'protected_transport', observed)
    compile_pipeline_prompts.draft_prompt(inputs)
    assert calls[compile_pipeline_operations.draft_base_prompt(inputs)] == 2


def test_invalid_policy_during_reuse_is_not_hidden(tmp_path, monkeypatch):
    import model_dlp
    import pytest

    inputs = _legacy_packet(tmp_path, monkeypatch, ['A durable exact fact.'])
    protecting_load = model_dlp.load_policy
    loads = []

    def failed_policy_after_protection():
        loads.append(1)
        if len(loads) == 1:
            return protecting_load()
        raise model_dlp.DLPPolicyError('policy unavailable')

    monkeypatch.setattr(model_dlp, 'load_policy', failed_policy_after_protection)
    with pytest.raises(model_dlp.DLPPolicyError):
        compile_pipeline_prompts.draft_layout(inputs)


def test_cross_boundary_secret_still_changes_prefix(tmp_path, monkeypatch):
    import pytest

    from tests.compile_binds_the_protected_source_view_helpers import _policy

    inputs = _legacy_packet(tmp_path, monkeypatch, ['A durable exact fact.'])
    base = compile_pipeline_operations.draft_base_prompt(inputs)
    _policy(monkeypatch, tmp_path, literals=(base[-8:] + '\n\nLEGACY',))
    with pytest.raises(ValueError, match='changed protected source prefix'):
        compile_pipeline_prompts.draft_layout(inputs)


def test_final_scan_failure_resets_layout_scope(tmp_path, monkeypatch):
    import pytest

    inputs = _legacy_packet(tmp_path, monkeypatch, ['A durable exact fact.'])
    original = llm_client.redact_for_transport

    def checked(text, policy):
        if 'LEGACY SOURCE ADDRESSES' in text:
            raise RuntimeError('scanner failed')
        return original(text, policy)

    monkeypatch.setattr(llm_client, 'redact_for_transport', checked)
    with pytest.raises(ValueError, match='blocked by DLP'):
        compile_pipeline_prompts.draft_layout(inputs)
    assert compile_pipeline_operations.LAYOUT_PROTECTION.get() is None


def test_nested_layout_scope_restores_outer_exact_base(monkeypatch):
    calls = Counter()
    original = llm_client.protected_transport

    def observed(system, prompt, schema):
        calls[prompt] += 1
        return original(system, prompt, schema)

    monkeypatch.setattr(llm_client, 'protected_transport', observed)
    with compile_pipeline_prompts.layout_protection_scope():
        compile_pipeline_operations.choice_base_transport('outer')
        with compile_pipeline_prompts.layout_protection_scope():
            compile_pipeline_operations.choice_base_transport('inner')
        compile_pipeline_operations.choice_base_transport('outer')
    assert calls == {'outer': 1, 'inner': 1}
    compile_pipeline_operations.choice_base_transport('outer')
    assert calls['outer'] == 2


def test_blocked_result_is_never_retained(monkeypatch):
    calls = []

    def blocked(system, prompt, schema):
        calls.append(prompt)
        return llm_client.Blocked('dlp_scan_error', 'scanner failed')

    monkeypatch.setattr(llm_client, 'protected_transport', blocked)
    with compile_pipeline_prompts.layout_protection_scope():
        compile_pipeline_operations.choice_base_transport('same base')
        compile_pipeline_operations.choice_base_transport('same base')
    assert calls == ['same base', 'same base']
