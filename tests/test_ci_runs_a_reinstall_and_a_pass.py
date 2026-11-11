"""CI installs twice and runs a pass on what it installed (audit 2026-09-26 C-13).

docs/research/2026-09-26-ci-runs-a-reinstall-and-a-pass.md
"""
from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "tests.yml"


def _end_to_end_commands() -> list[str]:
    job = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["install-end-to-end"]
    return [str(step["run"]) for step in job["steps"] if "run" in step]


def test_each_installer_runs_twice_before_the_check() -> None:
    commands = _end_to_end_commands()
    check = next(index for index, command in enumerate(commands) if "repair_installed_memory.py --check" in command)
    before = commands[:check]

    assert [
        sum("install.sh --scheduler cron" in command for command in before),
        sum("install.ps1" in command for command in before),
    ] == [2, 2]


def test_a_nightly_pass_runs_on_the_installed_vault() -> None:
    commands = _end_to_end_commands()

    assert commands[-1] == "uv run --locked --no-sync python -u scripts/scheduled_nightly.py"


def test_failure_diagnostics_use_canonical_runtime_path_after_nightly():
    job = yaml.safe_load(WORKFLOW.read_text(encoding='utf-8'))['jobs']['install-end-to-end']
    steps = job['steps']
    normalize = next(i for i, step in enumerate(steps)
                     if step.get('name') == 'Canonicalize the install runtime path for artifact upload')
    install = next(i for i, step in enumerate(steps) if step.get('name') == 'Run install.sh end to end')
    assert normalize < install
    assert "pathlib.Path(os.environ['LLM_WIKI_STATE_ROOT']).resolve()" in steps[normalize]['run']
    assert 'GITHUB_ENV' in steps[normalize]['run']
    artifact = steps[-1]
    assert artifact['if'] == 'failure()'
    assert artifact['uses'].startswith('actions/upload-artifact@')
    assert artifact['with']['path'] == '${{ env.LLM_WIKI_STATE_ROOT }}/logs/capture-failures.jsonl'
