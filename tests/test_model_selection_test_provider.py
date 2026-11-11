"""Explicit fake mode needs no persisted model or subscription access."""
import json
from unittest.mock import Mock

import choose_model
import pytest


@pytest.fixture
def fake_selection(monkeypatch):
    monkeypatch.setenv('MEMORY_LLM_PROVIDER', 'fake')
    provider = Mock(side_effect=AssertionError('must not discover a provider'))
    monkeypatch.setattr(choose_model, 'discovery_provider', provider)
    return provider


@pytest.mark.parametrize('saved', [b'', b'[llm]\nmodel="claude/sonnet"\n'])
def test_default_select_preserves_settings_without_provider_access(
    fake_selection, tmp_path, capsys, saved,
):
    path = tmp_path / 'llm-wiki.toml'
    path.write_bytes(saved)
    assert choose_model.main(['--root', str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        'status': 'test-provider', 'provider': 'fake', 'model': '',
    }
    assert path.read_bytes() == saved
    fake_selection.assert_not_called()


def test_fresh_test_selection_creates_no_settings(fake_selection, tmp_path, capsys):
    assert choose_model.main(['--root', str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'test-provider'
    assert not (tmp_path / 'llm-wiki.toml').exists()
    fake_selection.assert_not_called()


@pytest.mark.parametrize('requested', [['--model', 'sonnet'], ['--reasoning', 'high']])
def test_fake_mode_refuses_explicit_model_settings(fake_selection, tmp_path, capsys, requested):
    assert choose_model.main(['--root', str(tmp_path), *requested]) == 1
    assert 'cannot select a model or reasoning' in capsys.readouterr().err
    assert not (tmp_path / 'llm-wiki.toml').exists()
    fake_selection.assert_not_called()


def test_explicit_real_provider_still_reaches_protected_verification(monkeypatch, tmp_path):
    monkeypatch.setenv('MEMORY_LLM_PROVIDER', 'fake')
    descriptor = choose_model.llm_client.ProviderDescriptor('claude', 'sonnet', {}, {}, 0, ())
    monkeypatch.setattr(choose_model, 'discovery_provider', lambda provider: descriptor)
    verify = Mock(side_effect=ValueError('verification refused'))
    monkeypatch.setattr(choose_model, 'verified_selection', verify)
    assert choose_model.main(['--root', str(tmp_path), '--provider', 'claude', '--model', 'sonnet']) == 1
    verify.assert_called_once()
    assert not (tmp_path / 'llm-wiki.toml').exists()
