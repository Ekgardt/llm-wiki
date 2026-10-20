"""The dependency witness must bind live configuration, not just source versions."""
import hashlib
import json
import os

import compile_controller as controller
import llm_client
import model_dlp
import model_selection
import pytest
import settings
from reliable_memory import canonical_json_bytes

from tests.test_compile_call_controller import work as work
from tests.test_compile_progress_admission import upgraded as upgraded
from tests.test_compile_protected_dispatch import provider as provider


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.delenv('LLM_WIKI_DLP_POLICY', raising=False)
    monkeypatch.delenv('LLM_WIKI_COMPILE_VALIDATION_RETRIES', raising=False)
    return tmp_path


def _configuration(root):
    return controller.compile_configuration_dependency_manifest(root)


def _settings(root, reasoning='medium'):
    path = root / 'llm-wiki.toml'
    path.write_text(f'[llm]\nmodel="codex/gpt-6.1-sol"\nreasoning="{reasoning}"\n')
    return path


def _policy(root, literal='private-literal'):
    path = root / 'policy.json'
    value = {'version': 1, 'literals': [literal], 'allow_fingerprints': []}
    value['sha256'] = hashlib.sha256(canonical_json_bytes(value)).hexdigest()
    path.write_text(json.dumps(value))
    return path


def test_configuration_detects_same_stat_settings_change(root):
    path = _settings(root)
    before = _configuration(root)
    stat = path.stat()
    path.write_bytes(path.read_bytes().replace(b'medium', b'xhigh '))
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    with pytest.raises(ValueError):
        _configuration(root)
    path.write_bytes(path.read_bytes().replace(b'xhigh ', b'xhighx'))
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert _configuration(root) != before


def test_effective_override_and_inherited_environment_change_identity(root, monkeypatch):
    original = _configuration(root)
    monkeypatch.setenv('LLM_WIKI_COMPILE_VALIDATION_RETRIES', '4')
    changed = _configuration(root)
    assert changed != original
    monkeypatch.setenv('PROVIDER_CUSTOM_INPUT', 'changed')
    assert _configuration(root) != changed


def test_configuration_contains_no_environment_or_policy_secrets(root, monkeypatch):
    monkeypatch.setenv('LLM_WIKI_DLP_POLICY', str(_policy(root)))
    monkeypatch.setenv('MEMORY_LLM_API_KEY', 'private-api-token')
    encoded = json.dumps(_configuration(root))
    assert 'private-literal' not in encoded
    assert 'private-api-token' not in encoded
    assert str(root) not in encoded


def test_policy_change_changes_identity_and_invalid_required_policy_refuses(root, monkeypatch):
    path = _policy(root)
    monkeypatch.setenv('LLM_WIKI_DLP_POLICY', str(path))
    original = _configuration(root)
    _policy(root, 'other-literal')
    assert _configuration(root) != original
    path.write_text('{}')
    with pytest.raises(model_dlp.DLPPolicyError):
        _configuration(root)


def test_scoped_authoritative_model_is_bound(root):
    first = model_selection.selection('codex/gpt-6.1-sol', 'medium')
    second = model_selection.selection('codex/gpt-6.1-sol', 'low')
    with model_selection.using(first):
        original = _configuration(root)
    with model_selection.using(second):
        assert _configuration(root) != original


def test_change_during_configuration_read_refuses_mixed_identity(root, monkeypatch):
    original = settings.effective

    def changing(*args, **kwargs):
        result = original(*args, **kwargs)
        monkeypatch.setenv('PROVIDER_CUSTOM_INPUT', os.environ.get('PROVIDER_CUSTOM_INPUT', '') + 'x')
        return result

    monkeypatch.setattr(settings, 'effective', changing)
    with pytest.raises(controller.progress.ProgressConflict, match='configuration changed'):
        _configuration(root)


def _components(monkeypatch, root):
    product = root / 'product'
    product.write_bytes(b'original')
    monkeypatch.setattr(controller, 'compile_product_dependency_manifest',
        lambda root, parameters, **kw: controller.compile_dependency_manifest({'product': product}, parameters))
    monkeypatch.setattr(controller, 'compile_package_dependency_manifest', lambda **kw: {'package': 'fixture'})
    monkeypatch.setattr(controller, 'compile_runtime_dependency_manifest', lambda **kw: {'python': 'fixture'})
    return product


def test_composed_witness_rechecks_each_component_and_configuration(root, monkeypatch):
    product = _components(monkeypatch, root)
    witness = controller.capture_compile_dependencies(root, {'request': 'initial'})
    assert witness.verify() == witness.digest
    product.write_bytes(b'changed')
    with pytest.raises(controller.progress.ProgressConflict, match='dependency identity'):
        witness.verify()
    product.write_bytes(b'original')
    monkeypatch.setenv('PROVIDER_CUSTOM_INPUT', 'changed')
    with pytest.raises(controller.progress.ProgressConflict, match='dependency identity'):
        witness.verify()


def test_configuration_change_during_inventory_cannot_be_admitted(root, monkeypatch):
    _components(monkeypatch, root)

    def changing(**kwargs):
        monkeypatch.setenv('PROVIDER_CUSTOM_INPUT', 'changed-during-inventory')
        return {'package': 'fixture'}

    monkeypatch.setattr(controller, 'compile_package_dependency_manifest', changing)
    with pytest.raises(controller.progress.ProgressConflict, match='configuration changed'):
        controller.capture_compile_dependencies(root, {})


def test_expired_inventory_does_not_read_configuration(root, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('expired inventory started reading')

    monkeypatch.setattr(settings, 'effective', forbidden)
    with pytest.raises(TimeoutError):
        controller.compile_configuration_dependency_manifest(root, deadline=0)


def test_configuration_change_during_call_retains_answer_without_semantic_use(
    work, provider, root, monkeypatch,
):
    _components(monkeypatch, work[0].vault)
    witness = controller.capture_compile_dependencies(work[0].vault, {})
    calls = []

    with monkeypatch.context() as change:
        def backend(*args):
            calls.append(True)
            change.setenv('MEMORY_LLM_API_KEY', 'changed-during-call')
            return 'answer'

        change.setitem(llm_client._BACKENDS, 'fake', backend)
        with pytest.raises(controller.progress.ProgressConflict, match='dependency identity'):
            controller.run_candidate_call(work[0], work[1], work[2], dependencies=witness,
                descriptor=provider, prompt='task', system_prompt='system')
    assert work[0].get_compile_event(work[2].work_id).details['model_call']['phase'] == 'answer'
    result = controller.run_candidate_call(work[0], work[1], work[2], dependencies=witness,
        descriptor=provider, prompt='task', system_prompt='system')
    assert result.text == 'answer'
    assert calls == [True]
