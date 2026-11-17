"""Helpers shared by test_compile_progress_installers.py and the test files importing them."""

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.installer_bootstrap_helpers import _powershell_functions, _shell_function
from tests.powershell_literal import ps_literal

ROOT = Path(__file__).resolve().parents[1]


SHELL_NAMES = ("compile_progress_state_of", "apply_compile_progress_upgrade",
               "upgrade_compile_progress", "adoption_tail")


PS_NAMES = ("Get-CompileProgressState", "Invoke-CompileProgressMigration",
            "Invoke-CompileProgressUpgrade")


def _shim(tmp_path):
    binary = tmp_path / "uv-shim.py"
    binary.write_text("""
import json, os, subprocess, sys
with open(os.environ['TEST_CALLS'], 'a', encoding='utf-8') as out:
    out.write(json.dumps(sys.argv[1:]) + '\\n')
if os.environ.get('TEST_RUN_REAL') == '1':
    sys.exit(subprocess.run([sys.executable, *sys.argv[5:]], check=False).returncode)
if '--check' in sys.argv:
    print(json.dumps({'details': {'compile_progress': os.environ['TEST_PROGRESS']}}))
    sys.exit(0)
print('migration diagnostic')
sys.exit(int(os.environ['TEST_MIGRATION_EXIT']))
""", encoding="utf-8")
    return binary


def _shim_function(platform, shim):
    python = Path(sys.executable).as_posix()
    path = shim.as_posix()
    functions = {
        'bash': f'uv() {{ {shlex.quote(python)} {shlex.quote(path)} "$@"; }}\n',
        'pwsh': f'function uv {{ & {ps_literal(python)} {ps_literal(path)} @args }}\n',
    }
    return functions[platform]


def _shell_script(state, image, confirmed):
    source = (ROOT / "install.sh").read_text()
    functions = "\n".join(_shell_function(source, name) for name in SHELL_NAMES)
    return f"""
set -euo pipefail
BOOTSTRAP_PYTHON={shlex.quote(sys.executable)}
VAULT_ROOT={shlex.quote(str(ROOT))}
STATE_ROOT={shlex.quote(str(state))}
ADOPTION_ERR="$STATE_ROOT/logs/install-adoption.err.log"
COMPILE_BACKUP_IMAGE={shlex.quote(str(image))}
AGENTS_STOPPED={int(confirmed)}
SYNC_WARNING=0
ok() {{ echo "$1"; }}
warn() {{ echo "$1"; }}
{functions}
upgrade_compile_progress
echo "WARNING:$SYNC_WARNING"
"""


def _powershell_script(state, image, confirmed):
    return _powershell_functions(ROOT / "install.ps1", PS_NAMES) + f"""
$ErrorActionPreference = 'Stop'
$VAULT_ROOT = {ps_literal(str(ROOT))}
$STATE_ROOT = {ps_literal(str(state))}
$CompileBackupImage = {ps_literal(str(image))}
$ConfirmAllAgentsStopped = ${str(confirmed).lower()}
$syncWarning = $false
function Ok($msg) {{ Write-Output $msg }}
function Warn($msg) {{ Write-Output $msg }}
Invoke-CompileProgressUpgrade
Write-Output "WARNING:$([int]$syncWarning)"
"""


def _run_slice(tmp_path, platform, state, image, confirmed, **settings):
    executable = shutil.which(platform)
    if executable is None:
        pytest.skip(f"{platform} unavailable")
    shim = _shim(tmp_path)
    (state / "logs").mkdir(parents=True, exist_ok=True)
    calls = tmp_path / "calls.jsonl"
    env = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ["PATH"],
               TEST_CALLS=str(calls), TEST_PROGRESS="upgrade-required",
               TEST_MIGRATION_EXIT="0")
    env.update(settings)
    scripts = {"bash": (_shell_script, ["-c"]), "pwsh": (_powershell_script, ["-NoProfile", "-NonInteractive", "-Command"])}
    render, flags = scripts[platform]
    source = _shim_function(platform, shim) + render(state, image, confirmed)
    result = subprocess.run([executable, *flags, source],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    return result, [json.loads(line) for line in calls.read_text(encoding='utf-8').splitlines()]
