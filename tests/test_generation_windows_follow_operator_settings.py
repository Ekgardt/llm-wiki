"""Generation work and external schedulers must honor the same measured window."""
import json
import re

import compile_memory
import generation_maintenance
import install_state
import nightly_budget
import pytest
import scheduled_nightly
import settings
from llm_wiki import doctor_schedule, install_scheduler_render, install_windows
from reliable_memory import canonical_json_bytes


def test_both_builders_read_the_vault_settings(tmp_path, monkeypatch):
    (tmp_path / 'llm-wiki.toml').write_text('[generation]\nnightly_seconds = 2117\npost_compile_seconds = 2119\n')
    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
    calls = []
    monkeypatch.setattr(scheduled_nightly, 'run_generation_maintenance', lambda **kw: calls.append(kw['time_budget_seconds']) or {'status':'current'})
    monkeypatch.setattr(generation_maintenance, 'run_generation_maintenance', lambda *args, **kw: calls.append(kw['time_budget_seconds']) or {'status':'current'})
    scheduled_nightly.generation_result()
    compile_memory.refresh_generation_after_compile()
    assert calls == [2117, 2119]


def test_the_nightly_total_reloads_a_changed_window(monkeypatch):
    monkeypatch.setenv('LLM_WIKI_GENERATION_NIGHTLY_SECONDS', '2117')
    before = nightly_budget.worst_case_seconds()
    monkeypatch.setenv('LLM_WIKI_GENERATION_NIGHTLY_SECONDS', '3117')
    assert nightly_budget.worst_case_seconds() - before == 1000


def test_schedulers_outlast_the_configured_generation(tmp_path, monkeypatch):
    monkeypatch.setenv('LLM_WIKI_GENERATION_NIGHTLY_SECONDS', '50000')
    rendered = install_scheduler_render.render_systemd_definitions(tmp_path, tmp_path, tmp_path / 'uv')
    limit = re.search(r'^TimeoutStartSec=(\d+)h$', rendered['llm-wiki-nightly.service'].decode(), re.M)
    windows = json.loads(install_windows.render_windows_task_spec(tmp_path, tmp_path, tmp_path / 'uv'))
    windows_limit = windows['tasks'][0]['limit_hours'] * 3600
    assert int(limit.group(1)) * 3600 > 50000
    assert windows_limit > 50000


def test_a_saved_windows_definition_keeps_its_window_after_settings_change(tmp_path, monkeypatch):
    monkeypatch.setenv('LLM_WIKI_GENERATION_NIGHTLY_SECONDS', '50000')
    saved = install_windows.render_windows_task_spec(tmp_path, tmp_path, tmp_path / 'uv')
    value = json.loads(saved)
    monkeypatch.setenv('LLM_WIKI_GENERATION_NIGHTLY_SECONDS', '70000')
    decoded = install_windows.decode_windows_task_spec(saved, tmp_path, tmp_path)
    command = install_windows.windows_task_command_from_spec(decoded, powershell='pwsh', script_path=tmp_path/'tasks.ps1', mode=None)
    assert int(command[command.index('-NightlyLimitHours') + 1]) == value['tasks'][0]['limit_hours']


def test_an_invalid_window_is_not_silently_ignored(monkeypatch):
    monkeypatch.setenv('LLM_WIKI_GENERATION_POST_COMPILE_SECONDS', 'wrong')
    monkeypatch.setattr(generation_maintenance, 'run_generation_maintenance', lambda *args, **kw: {'status':'current'})
    with pytest.raises(settings.SettingsError, match='LLM_WIKI_GENERATION_POST_COMPILE_SECONDS'):
        compile_memory.refresh_generation_after_compile()


@pytest.mark.parametrize(('options', 'expected'), [
    ('-NightlyLimitHours 17 -WeeklyLimitHours 7', [[17, True], [7, True]]),
    ('', [[install_scheduler_render.SCHEDULER_LIMIT_HOURS['nightly'], True],
          [install_scheduler_render.SCHEDULER_LIMIT_HOURS['weekly'], True]]),
])
def test_powershell_registers_the_supplied_windows_limits(tmp_path, options, expected):
    from tests.a_changed_task_setting_reaches_an_installed_machine_helpers import (
        SCRIPT,
        STUBS,
        _run,
    )
    from tests.powershell_literal import ps_literal

    (tmp_path / 'scripts').mkdir()
    (tmp_path / 'scripts' / 'run-scheduled-task.ps1').write_text('')
    (tmp_path / 'uv.exe').write_text('')
    command = STUBS + (
        f"\n. {ps_literal(str(SCRIPT))} -VaultRoot {ps_literal(str(tmp_path))} "
        f"-StateRoot {ps_literal(str(tmp_path))} -UvPath {ps_literal(str(tmp_path / 'uv.exe'))} "
        f'{options} 6>$null\n'
        'ConvertTo-Json -Compress $script:registered\n'
    )
    result = _run(command)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == expected


@pytest.mark.parametrize('hours', [True, 0, -1, '17', None])
def test_a_saved_windows_limit_is_validated(tmp_path, hours):
    value = json.loads(install_windows.render_windows_task_spec(tmp_path, tmp_path, tmp_path/'uv'))
    value['tasks'][0]['limit_hours'] = hours
    with pytest.raises(install_state.InstallControlError, match='install_windows_task_spec_invalid'):
        install_windows.decode_windows_task_spec(canonical_json_bytes(value), tmp_path, tmp_path)


def test_doctor_uses_the_checked_vault_and_not_another_vault(tmp_path, monkeypatch):
    monkeypatch.delenv('XDG_CONFIG_HOME', raising=False)
    vault = tmp_path / 'vault'
    vault.mkdir()
    (vault / 'llm-wiki.toml').write_text('[generation]\nnightly_seconds = 50000\n')
    home = tmp_path / 'home'
    units = home / '.config' / 'systemd' / 'user'
    units.mkdir(parents=True)
    definitions = install_scheduler_render.render_systemd_definitions(vault, vault, vault/'uv')
    (units / 'llm-wiki-nightly.service').write_bytes(definitions['llm-wiki-nightly.service'])
    assert doctor_schedule.unit_limit_verdict(home, vault) is None
    (vault / 'llm-wiki.toml').write_text('[generation]\nnightly_seconds = 70000\n')
    verdict = doctor_schedule.unit_limit_verdict(home, vault)
    assert verdict[0] == 'degraded' and 'rerun the installer' in verdict[1]
