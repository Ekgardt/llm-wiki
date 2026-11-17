"""Both installer entrypoints carry the offline gate and exact backup argument."""
import json
import os
import shutil
import sqlite3
import subprocess
import sys

import installed_memory_repair as repair
import pytest

from tests.compile_progress_installers_helpers import ROOT, _run_slice, _shim
from tests.legacy_adopted_vault import adopt_legacy as adopt


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


def test_real_shim_waits_and_preserves_arguments_and_failure(tmp_path):
    child = tmp_path / 'child with spaces.py'
    output = tmp_path / 'result with spaces.json'
    child.write_text(
        "import json, pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]), encoding='utf-8')\n"
        "sys.exit(7)\n", encoding='utf-8',
    )
    arguments = ['space value', 'quote"value', 'back\\slash']
    env = dict(os.environ, TEST_RUN_REAL='1', TEST_CALLS=str(tmp_path / 'calls.jsonl'))
    result = subprocess.run(
        [sys.executable, str(_shim(tmp_path)), 'run', '--locked', '--no-sync', 'python',
         str(child), str(output), *arguments], env=env, check=False,
    )
    assert result.returncode == 7
    assert json.loads(output.read_text(encoding='utf-8')) == arguments
