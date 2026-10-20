"""Both installer entrypoints carry the offline gate and exact backup argument."""
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import installed_memory_repair as repair
import pytest

from tests.adopted_vault import adopt
from tests.powershell_literal import ps_literal
from tests.test_installer_bootstrap import _powershell_functions, _shell_function

ROOT = Path(__file__).resolve().parents[1]
SHELL_NAMES = ("compile_progress_state_of", "apply_compile_progress_upgrade",
               "upgrade_compile_progress", "adoption_tail")
PS_NAMES = ("Get-CompileProgressState", "Invoke-CompileProgressMigration",
            "Invoke-CompileProgressUpgrade")


def _shim(tmp_path):
    binary = tmp_path / "uv"
    binary.write_text(f"#!{sys.executable}\n" + """
import json, os, sys
with open(os.environ['TEST_CALLS'], 'a') as out:
    out.write(json.dumps(sys.argv[1:]) + '\\n')
if os.environ.get('TEST_RUN_REAL') == '1':
    os.execv(sys.executable, [sys.executable, *sys.argv[5:]])
if '--check' in sys.argv:
    print(json.dumps({'details': {'compile_progress': os.environ['TEST_PROGRESS']}}))
    sys.exit(0)
print('migration diagnostic')
sys.exit(int(os.environ['TEST_MIGRATION_EXIT']))
""")
    binary.chmod(0o700)
    return binary


def _shell_script(state, image, confirmed):
    source = (ROOT / "install.sh").read_text()
    functions = "\n".join(_shell_function(source, name) for name in SHELL_NAMES)
    return f"""
set -euo pipefail
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
    _shim(tmp_path)
    (state / "logs").mkdir(parents=True, exist_ok=True)
    calls = tmp_path / "calls.jsonl"
    env = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ["PATH"],
               TEST_CALLS=str(calls), TEST_PROGRESS="upgrade-required",
               TEST_MIGRATION_EXIT="0")
    env.update(settings)
    scripts = {"bash": (_shell_script, ["-c"]), "pwsh": (_powershell_script, ["-NoProfile", "-NonInteractive", "-Command"])}
    render, flags = scripts[platform]
    result = subprocess.run([executable, *flags, render(state, image, confirmed)],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    return result, [json.loads(line) for line in calls.read_text().splitlines()]


@pytest.mark.parametrize("platform", ["bash", "pwsh"])
@pytest.mark.parametrize("progress,image_supplied,confirmed,exit_code,warning,applies", [
    ("upgraded", False, False, 0, 0, 0),
    ("upgrade-required", False, True, 0, 1, 0),
    ("upgrade-required", True, False, 0, 1, 0),
    ("upgrade-required", True, True, 0, 0, 1),
    ("unknown", True, True, 0, 0, 1),
    ("upgrade-required", True, True, 2, 1, 1),
])
def test_installer_gate_and_argument_boundaries(
    tmp_path, platform, progress, image_supplied, confirmed, exit_code, warning, applies,
):
    image = str(tmp_path / "backup with spaces") if image_supplied else ""
    result, calls = _run_slice(
        tmp_path, platform, tmp_path / "state", image, confirmed,
        TEST_PROGRESS=progress, TEST_MIGRATION_EXIT=str(exit_code),
    )
    migrations = [call for call in calls if "--apply" in call]
    assert len(migrations) == applies
    assert f"WARNING:{warning}" in result.stdout
    for call in migrations:
        assert call[-5:] == ["--apply", "--upgrade-compile-progress", "--backup-image",
                             image, "--confirm-all-agents-stopped"]


@pytest.mark.parametrize("platform", ["bash", "pwsh"])
def test_installer_functions_upgrade_a_real_queue(tmp_path, platform):
    root, state = adopt(tmp_path)
    before = repair.inspect_installed_vault(root=root, state_root=state)
    assert before["details"]["compile_progress"] == "upgrade-required"
    image = tmp_path / "backup with spaces"
    image.mkdir(mode=0o700)
    result, calls = _run_slice(
        tmp_path, platform, state, image, True, TEST_RUN_REAL="1",
        LLM_WIKI_ROOT=str(root), LLM_WIKI_STATE_ROOT=str(state),
    )
    assert "WARNING:0" in result.stdout
    assert len([call for call in calls if "--apply" in call]) == 1
    after = repair.inspect_installed_vault(root=root, state_root=state)
    assert after["details"]["compile_progress"] == "upgraded"
    assert after["overall_status"] == "ok"


@pytest.mark.parametrize("platform", ["bash", "pwsh"])
def test_image_without_confirmation_is_rejected_before_installation(tmp_path, platform):
    executable = shutil.which(platform)
    if executable is None:
        pytest.skip(f"{platform} unavailable")
    arguments = {
        "bash": [str(ROOT / "install.sh"), "--compile-backup-image", str(tmp_path / "copy")],
        "pwsh": ["-NoProfile", "-NonInteractive", "-File", str(ROOT / "install.ps1"),
                 "-CompileBackupImage", str(tmp_path / "copy")],
    }
    result = subprocess.run([executable, *arguments[platform]], cwd=tmp_path,
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert "requires" in result.stdout + result.stderr
    assert list(tmp_path.iterdir()) == []


def test_windows_remote_bootstrap_preserves_image_argument():
    source = (ROOT / "install.ps1").read_text()
    assert '$reexecArguments += @("-CompileBackupImage", $CompileBackupImage)' in source


def test_inspection_does_not_announce_readiness_for_an_invalid_queue(tmp_path):
    root, state = adopt(tmp_path)
    with sqlite3.connect(state / "run/queue-v3.sqlite3") as database:
        database.execute("PRAGMA user_version=999")
    report = repair.inspect_installed_vault(root=root, state_root=state)
    assert report["overall_status"] == "error"
    assert "compile_progress" not in report["details"]
