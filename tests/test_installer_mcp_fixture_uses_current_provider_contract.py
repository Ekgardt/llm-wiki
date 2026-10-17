"""Installer fixtures distinguish current equivalence from owned legacy migration."""
import json

import codex_memory
import integration_hook_config as hooks
import pytest

from tests.test_integration_injection import _codex_mcp_toml


@pytest.mark.parametrize('quoted', [False, True])
def test_current_installer_fixture_is_exactly_equivalent(tmp_path, monkeypatch, quoted):
    monkeypatch.setenv('MEMORY_LLM_PROVIDER', 'codex')
    monkeypatch.setenv('MEMORY_CODEX_MODEL', 'gpt-6-luna')
    monkeypatch.setenv('MEMORY_CODEX_REASONING', 'max')
    config = tmp_path / 'config.toml'
    config.write_text(_codex_mcp_toml(tmp_path, quoted=quoted), encoding='utf-8')
    before = config.read_bytes()
    assert codex_memory.codex_mcp_config_state(config, tmp_path) == 'equivalent'
    assert config.read_bytes() == before


def test_old_direct_installer_fixture_is_owned_stale_not_equivalent(tmp_path):
    config = tmp_path / 'config.toml'
    args = ['run', '--locked', '--no-sync', '--directory', str(tmp_path),
            'python', 'scripts/mcp_server.py']
    config.write_text('[mcp_servers."llm-wiki"]\ncommand="uv"\nargs=' + json.dumps(args),
                      encoding='utf-8')
    before = config.read_bytes()
    assert codex_memory.codex_mcp_config_state(config, tmp_path, {}) == 'stale'
    assert config.read_bytes() == before


def test_current_entry_with_different_installed_bundle_is_owned_stale(tmp_path):
    config = tmp_path / 'config.toml'
    args = hooks.codex_launch_args(tmp_path, 'mcp_server.py', (), {}, relative_target=True)
    config.write_text('[mcp_servers.llm-wiki]\ncommand="uv"\nargs=' + json.dumps(args),
                      encoding='utf-8')
    assert codex_memory.codex_mcp_config_state(config, tmp_path,
                                              {'MEMORY_LLM_PROVIDER': 'codex'}) == 'stale'
