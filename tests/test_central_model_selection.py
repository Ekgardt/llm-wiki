"""All memory entrypoints must resolve the saved choice, not inherited copies."""
import llm_client
import pytest
import settings


@pytest.fixture
def central(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'default_root', lambda: tmp_path)
    monkeypatch.setenv('MEMORY_LLM_PROVIDER', 'claude')
    monkeypatch.setenv('MEMORY_CODEX_MODEL', 'stale-model')
    monkeypatch.setenv('MEMORY_CODEX_REASONING', 'low')
    path = tmp_path / 'llm-wiki.toml'
    path.write_text('[llm]\nmodel = "codex/gpt-6.1-sol"\nreasoning = "medium"\n')
    return path


def test_saved_selection_overrides_copied_provider_and_model(central):
    assert llm_client.forced_provider() == 'codex'
    descriptor, = llm_client.provider_candidates(llm_client.forced_provider())
    assert descriptor.model == 'gpt-6.1-sol'
    assert descriptor.inference_settings['reasoning'] == 'medium'


def test_long_lived_process_observes_updated_selection(central):
    first = llm_client.provider_candidates('codex')[0]
    central.write_text('[llm]\nmodel="codex/another-model"\nreasoning="high"\n')
    second = llm_client.provider_candidates('codex')[0]
    assert (first.model, second.model) == ('gpt-6.1-sol', 'another-model')
    assert second.inference_settings['reasoning'] == 'high'


def test_model_section_does_not_break_existing_limits(central):
    assert settings.setting_value('compile.validation_retries') == 2


def test_invalid_selection_does_not_fall_back_to_inherited_provider(central):
    central.write_text('[llm]\nmodel="misspelled/model"\n')
    with pytest.raises(ValueError):
        llm_client.forced_provider()


def test_candidate_resolution_uses_one_selection_snapshot(central, monkeypatch):
    import model_selection

    original = model_selection.selected_configuration

    def change_file_during_resolution(provider, configuration, chosen):
        central.write_text('[llm]\nmodel="claude/sonnet"\n')
        return original(provider, configuration, chosen)

    monkeypatch.setattr(model_selection, 'selected_configuration', change_file_during_resolution)
    descriptor, = llm_client.provider_candidates('codex')
    assert (descriptor.provider, descriptor.model) == ('codex', 'gpt-6.1-sol')
    assert llm_client.forced_provider() == 'claude'


def test_explicit_fake_never_uses_subscription_selection(central, monkeypatch):
    monkeypatch.setenv('MEMORY_LLM_PROVIDER', 'fake')
    assert llm_client.forced_provider() == 'fake'
    assert llm_client.provider_candidates('fake')[0].provider == 'fake'


def test_persisted_selection_is_not_projected_into_agents(central, monkeypatch):
    import integration_hook_config
    from model_selection import MODEL_ENV_KEYS

    monkeypatch.setenv('MEMORY_LLM_BASE_URL', 'http://127.0.0.1:11434/v1')
    monkeypatch.setenv('OLLAMA_NO_CLOUD', '1')
    projected = integration_hook_config.provider_environment()
    assert not MODEL_ENV_KEYS.intersection(projected)
    assert projected['MEMORY_LLM_BASE_URL'] == 'http://127.0.0.1:11434/v1'
    assert projected['OLLAMA_NO_CLOUD'] == '1'


@pytest.fixture
def editable(tmp_path, monkeypatch):
    from llm_wiki import model_choice

    root = tmp_path / 'vault'
    root.mkdir()
    monkeypatch.setenv('LLM_WIKI_STATE_ROOT', str(tmp_path / 'runtime'))
    path = root / 'llm-wiki.toml'
    path.write_text('# Keep budget explanation\n[compile]\nvalidation_retries = 7 # owner choice\n'
                    '[llm]\nmodel = "codex/old" # model explanation\nreasoning = "low"\n')
    return path, model_choice.selection('codex/gpt-6.1-sol', 'medium')


def test_save_preserves_comments_and_existing_budgets(editable):
    import model_selection
    import model_selection_file

    path, chosen = editable
    model_selection_file.save(chosen, expected=path.read_bytes(), root=path.parent)
    assert model_selection.current(path.parent) == chosen
    assert settings.setting_value('compile.validation_retries', root=path.parent) == 7
    assert '# Keep budget explanation' in path.read_text()
    assert '# owner choice' in path.read_text()
    assert '# model explanation' in path.read_text()


def test_concurrent_edit_is_not_overwritten_after_verification(editable):
    import model_selection_file

    path, chosen = editable
    original = path.read_bytes()
    changed = original.replace(b'retries = 7', b'retries = 8')
    path.write_bytes(changed)
    with pytest.raises(ValueError, match='settings changed'):
        model_selection_file.save(chosen, expected=original, root=path.parent)
    assert path.read_bytes() == changed


def test_failed_publication_keeps_previous_model(editable, monkeypatch):
    import model_selection_file

    path, chosen = editable
    original = path.read_bytes()

    def disk_full(*args, **kwargs):
        raise OSError('disk full')

    monkeypatch.setattr(model_selection_file, 'atomic_write', disk_full)
    with pytest.raises(OSError, match='disk full'):
        model_selection_file.save(chosen, expected=original, root=path.parent)
    assert path.read_bytes() == original


def test_switching_adapter_removes_inapplicable_reasoning(editable):
    import model_selection
    import model_selection_file
    from llm_wiki import model_choice

    path, _ = editable
    chosen = model_choice.selection('claude/sonnet')
    model_selection_file.save(chosen, expected=path.read_bytes(), root=path.parent)
    assert model_selection.current(path.parent) == chosen
    assert 'reasoning' not in settings.tomllib.loads(path.read_text(encoding='utf-8'))['llm']


def test_hidden_codex_models_are_not_offered(monkeypatch):
    import choose_model

    assert choose_model.named(choose_model.visible_models([
        {'slug': 'visible', 'visibility': 'list'}, {'slug': 'hidden', 'visibility': 'hide'},
    ])) == ['visible']


def test_unsupported_reasoning_refuses_before_inference(monkeypatch):
    import choose_model

    monkeypatch.setattr(choose_model, 'codex_catalog', lambda: [
        {'slug': 'test-model', 'default_reasoning_level': 'medium',
         'supported_reasoning_levels': [{'effort': 'medium'}]},
    ])
    with pytest.raises(ValueError, match='unsupported reasoning'):
        choose_model.codex_reasoning('test-model', 'invented')
    assert choose_model.codex_reasoning('test-model', None) == 'medium'


@pytest.fixture
def strict_local_only(central, monkeypatch):
    monkeypatch.setenv('MEMORY_LLM_PROVIDER', 'ollama')
    monkeypatch.setenv('OLLAMA_NO_CLOUD', '1')
    monkeypatch.setenv('MEMORY_LLM_BASE_URL', 'http://127.0.0.1:11434/v1')
    return central


def test_a_saved_cloud_model_is_refused_when_local_only_is_asked_for(strict_local_only):
    descriptor, = llm_client.provider_candidates(llm_client.forced_provider())

    assert (descriptor.model, descriptor.resolution_failure) == (None, 'local_only_conflict')
    assert not llm_client.probe_candidate(descriptor)
    answer = llm_client.call_candidate(descriptor, 'private vault text', '', available=True)
    assert (answer.text, answer.failure_class) == (None, 'local_only_conflict')


def test_a_saved_ollama_model_keeps_the_enforced_local_path(strict_local_only):
    strict_local_only.write_text('[llm]\nmodel = "ollama/qwen3"\n')

    descriptor, = llm_client.provider_candidates(llm_client.forced_provider())

    assert (descriptor.model, descriptor.resolution_failure) == ('qwen3', None)
    assert descriptor.capabilities['local_only_enforced'] is True
    assert llm_client.local_only_conflict() is None


def test_a_saved_cloud_model_is_used_when_local_only_was_not_asked_for(central, monkeypatch):
    monkeypatch.delenv('OLLAMA_NO_CLOUD', raising=False)

    descriptor, = llm_client.provider_candidates(llm_client.forced_provider())

    assert (descriptor.resolution_failure, llm_client.local_only_conflict()) == (None, None)


def test_doctor_names_the_local_only_conflict(strict_local_only, tmp_path):
    from llm_wiki import doctor_base

    for name in ('knowledge/notes', 'scripts'):
        (tmp_path / name).mkdir(parents=True)

    result = doctor_base.environment_check(tmp_path, tmp_path)

    assert result['status'] == 'degraded'
    assert 'codex/gpt-6.1-sol' in result['message'] and 'choose_model.py set ollama/' in result['message']
