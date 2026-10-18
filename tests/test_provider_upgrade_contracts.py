"""Upgrades must refresh observed capabilities without a release allowlist."""
import subprocess

import llm_client as lc


def test_claude_upgrade_at_same_path_refreshes_help(monkeypatch, tmp_path):
    executable = tmp_path / 'claude'
    executable.write_text('old')
    monkeypatch.setattr(lc.shutil, 'which', lambda name: str(executable))
    calls = []

    def help_reply(*args, **kwargs):
        calls.append(1)
        return subprocess.CompletedProcess([], 0, executable.read_text(), '')

    monkeypatch.setattr(lc.subprocess, 'run', help_reply)
    lc._probed_claude_flags.cache_clear()
    executable.write_text('--old-flag')
    assert lc._claude_cli_flags() == frozenset({'--old-flag'})
    executable.write_text('--new-isolation-flag')
    assert lc._claude_cli_flags() == frozenset({'--new-isolation-flag'})
    assert lc._claude_cli_flags() == frozenset({'--new-isolation-flag'})
    assert len(calls) == 2
    lc._probed_claude_flags.cache_clear()


def test_claude_replacement_during_help_is_not_cached(monkeypatch, tmp_path):
    executable = tmp_path / 'claude'
    executable.write_text('original')
    monkeypatch.setattr(lc.shutil, 'which', lambda name: str(executable))

    def changed_help(*args, **kwargs):
        executable.write_text('replacement program')
        return subprocess.CompletedProcess([], 0, '--old-isolation-flag', '')

    monkeypatch.setattr(lc.subprocess, 'run', changed_help)
    lc._probed_claude_flags.cache_clear()
    assert lc._claude_cli_flags() is None
    assert lc._probed_claude_flags.cache_info().currsize == 0


def test_removed_claude_does_not_reuse_cached_flags(monkeypatch, tmp_path):
    executable = tmp_path / 'claude'
    executable.write_text('program')
    monkeypatch.setattr(lc.shutil, 'which', lambda name: str(executable))
    monkeypatch.setattr(lc.subprocess, 'run', lambda *a, **k:
                        subprocess.CompletedProcess([], 0, '--tools', ''))
    lc._probed_claude_flags.cache_clear()
    assert lc._claude_cli_flags() == frozenset({'--tools'})
    executable.unlink()
    assert lc._claude_cli_flags() is None
    lc._probed_claude_flags.cache_clear()
