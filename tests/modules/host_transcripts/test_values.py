"""Host defaults and configured roots retain their ordered contract."""
from pathlib import Path

import pytest
from host_transcripts import host_transcript_roots


def test_the_defaults_stay_and_the_configured_directories_are_added(tmp_path):
    from host_transcripts import host_transcript_roots

    home = tmp_path / "home"
    environ = {"CLAUDE_CONFIG_DIR": str(tmp_path / "c"), "CODEX_HOME": str(tmp_path / "x")}

    assert host_transcript_roots(environ, home) == (
        home / ".claude" / "projects",
        tmp_path / "c" / "projects",
        home / ".codex" / "sessions",
        tmp_path / "x" / "sessions",
    )



@pytest.mark.parametrize('value', ['', ' ', '\t\n'])
def test_blank_configuration_does_not_remove_defaults(tmp_path, value):
    environment = {'CLAUDE_CONFIG_DIR': value, 'CODEX_HOME': value}
    assert host_transcript_roots(environment, tmp_path) == (
        tmp_path / '.claude/projects', tmp_path / '.codex/sessions',
    )


def test_duplicate_configured_defaults_appear_once(tmp_path):
    environment = {'CLAUDE_CONFIG_DIR': str(tmp_path / '.claude'),
                   'CODEX_HOME': str(tmp_path / '.codex')}
    assert host_transcript_roots(environment, tmp_path) == (
        tmp_path / '.claude/projects', tmp_path / '.codex/sessions',
    )


def test_configuration_strips_outer_space_and_expands_tilde(tmp_path):
    roots = host_transcript_roots({'CODEX_HOME': '  ~/moved-codex  '}, tmp_path)
    assert roots[-1] == Path.home() / 'moved-codex/sessions'
    assert len(roots) == 3


def test_explicit_environment_is_not_replaced_by_process_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'unexpected'))
    assert host_transcript_roots({}, tmp_path) == (
        tmp_path / '.claude/projects', tmp_path / '.codex/sessions',
    )
