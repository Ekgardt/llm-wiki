"""CI quality guards — catch documentation drift, undefined installer vars,
and benchmark/report consistency before they ship.

These tests enforce invariants that are easy to break silently:
  - skills must not reference the non-existent ``qmd`` CLI
  - install scripts must not use undefined variables
  - CHANGELOG version + test-count must match pyproject + live suite
  - architecture docs must not cite metrics absent from the benchmark report
  - skills' allowed-tools must only reference scripts that actually exist
  - README benchmark tables must not invent competitor numbers
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent


# ─── Helpers ────────────────────────────────────────────────────────

def _collect_test_count() -> int:
    """Return the live number of collected pytest tests."""
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    text = (r.stdout or "") + (r.stderr or "")
    m = re.search(r"(\d+)\s+tests?\s+collected", text)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s+selected", text)
    if m:
        return int(m.group(1))
    raise AssertionError(f"could not parse pytest collect count:\n{text[-500:]}")


def _init_installer_vault(path: Path, project_name: str = "llm-wiki") -> None:
    path.mkdir()
    (path / "pyproject.toml").write_text(
        f'[project]\nname = "{project_name}"\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://example.invalid/fetch"],
        cwd=path,
        check=True,
    )
    subprocess.run(
        ["git", "remote", "set-url", "--push", "origin", "https://example.invalid/push"],
        cwd=path,
        check=True,
    )


def _push_url(path: Path) -> str:
    result = subprocess.run(
        ["git", "remote", "get-url", "--push", "origin"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _extract_braced_function(source: str, name: str) -> str:
    start = source.index(name)
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"unterminated function: {name}")


def _find_bash() -> str | None:
    candidate = shutil.which("bash")
    if candidate and not (os.name == "nt" and "system32" in candidate.lower()):
        return candidate
    for path in (
        Path("C:/Program Files/Git/bin/bash.exe"),
        Path("C:/Program Files/Git/usr/bin/bash.exe"),
    ):
        if path.exists():
            return str(path)
    return None


def _path_for_bash(bash: str, path: Path) -> str:
    if os.name != "nt":
        return str(path)
    result = subprocess.run(
        [bash, "-c", 'cygpath -u "$1"', "--", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _simulate_crontab_command(command: str) -> tuple[str, str]:
    shell: list[str] = []
    stdin: list[str] = []
    destination = shell
    index = 0
    while index < len(command):
        if command[index:index + 2] == r"\%":
            destination.append("%")
            index += 2
            continue
        if command[index] == "%":
            destination = stdin
            destination.append("\n")
        else:
            destination.append(command[index])
        index += 1
    return "".join(shell), "".join(stdin)


def _run_shell_profile_update(
    bash: str,
    profile: Path,
    vault_root: str,
) -> None:
    source = (ROOT / "install.sh").read_text(encoding="utf-8")
    quote_function = _extract_braced_function(source, "shell_quote()")
    update_function = _extract_braced_function(source, "update_shell_profile()")
    command = (
        f"set -euo pipefail\n{quote_function}\n{update_function}\n"
        'update_shell_profile "$1" "$2" "$2" "opencode-sdk"'
    )
    subprocess.run(
        [bash, "-c", command, "--", _path_for_bash(bash, profile), vault_root],
        check=True,
        capture_output=True,
    )


POWERSHELL_CODEX_WRAPPER = '. "$env:LLM_WIKI_ROOT\\scripts\\codex-memory-wrapper.ps1"'


def _run_powershell_profile_update(
    profile: Path,
    *,
    source_transform=None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is unavailable")
    source = (ROOT / "install.ps1").read_text(encoding="utf-8")
    function = _extract_braced_function(source, "function Update-PowerShellProfile")
    if source_transform is not None:
        function = source_transform(function)
    env = {
        **os.environ,
        "LLM_WIKI_TEST_PROFILE": str(profile),
        "LLM_WIKI_TEST_WRAPPER": POWERSHELL_CODEX_WRAPPER,
    }
    command = (
        f"{function}\n"
        "$ErrorActionPreference = 'Stop'\n"
        "$changed = Update-PowerShellProfile "
        "-ProfilePath $env:LLM_WIKI_TEST_PROFILE "
        "-WrapperLine $env:LLM_WIKI_TEST_WRAPPER\n"
        "if ($changed) { 'changed' } else { 'unchanged' }"
    )
    return subprocess.run(
        [pwsh, "-NoProfile", "-Command", command],
        env=env,
        check=check,
        capture_output=True,
        text=True,
    )


def _write_successful_installer_stubs(fake_bin: Path) -> None:
    fake_bin.mkdir()
    (fake_bin / "python3").write_text("#!/bin/sh\necho 3.10\n", encoding="utf-8")
    (fake_bin / "uv").write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = --version ]; then echo 'uv 0-test'; exit 0; fi\n"
        "printf '%s\\t%s\\t%s\\n' \"${LLM_WIKI_ROOT-}\" "
        "\"${LLM_WIKI_STATE_ROOT-}\" \"$*\" >> \"$UV_LOG\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    (fake_bin / "crontab").write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = -l ]; then [ ! -f \"$CRONTAB_STORE\" ] || cat \"$CRONTAB_STORE\"; exit 0; fi\n"
        "if [ \"$1\" = - ]; then cat > \"$CRONTAB_STORE\"; exit 0; fi\n"
        "exit 2\n",
        encoding="utf-8",
    )
    (fake_bin / "git").write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = --version ]; then echo 'git version 0-test'; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    for name in ("python3", "uv", "crontab", "git"):
        (fake_bin / name).chmod(0o755)


# ─── 1. No qmd references in skills ─────────────────────────────────

def test_no_qmd_refs_in_skills():
    """The qmd CLI does not exist in this repo; skills must not reference it
    as a command. The conceptual tier name 'QMD' (matching lookup_mode.py)
    is allowed."""
    import re

    skills_dir = ROOT / "skills"
    hits: list[str] = []
    # Match qmd as a CLI command (e.g. `qmd status`, `qmd embed`) — not as
    # a standalone tier label like "| **QMD** |" or "## Tier: QMD".
    cli_re = re.compile(r"\bqmd\s+(?:status|embed|index|query|collections|build|sync)\b", re.IGNORECASE)
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        for i, line in enumerate(
            skill_md.read_text(encoding="utf-8").splitlines(), 1
        ):
            if cli_re.search(line):
                hits.append(f"{skill_md.relative_to(ROOT)}:{i}: {line.strip()}")
    assert not hits, "qmd CLI references found in skills (qmd CLI does not exist):\n" + "\n".join(hits)


def test_ci_uses_current_gitleaks_action():
    """Gitleaks must use the Node 24 action with an available scanner release."""
    workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    assert "gitleaks/gitleaks-action@e0c47f4f8be36e29cdc102c57e68cb5cbf0e8d1e" in workflow
    assert "GITLEAKS_VERSION: 8.30.1" in workflow


# ─── 2. install.ps1 — no undefined PowerShell variables ─────────────

def test_install_ps1_no_undefined_vars():
    """Every $var referenced in install.ps1 must be assigned or a known automatic."""
    content = (ROOT / "install.ps1").read_text(encoding="utf-8")

    skip = {
        "_", "args", "LASTEXITCODE", "PROFILE", "env", "PSScriptRoot",
        "ErrorActionPreference", "true", "false", "null", "input",
    }

    # Collect all $varName references.
    refs: set[str] = set()
    for m in re.finditer(r"\$([A-Za-z_]\w*)", content):
        var = m.group(1)
        if var in skip:
            continue
        refs.add(var)

    # Collect assignments: $var = ...
    assigned: set[str] = set()
    for m in re.finditer(r"\$([A-Za-z_]\w*)\s*=", content):
        assigned.add(m.group(1))

    # Collect function parameters: function Name($a, $b)
    for fm in re.finditer(r"function\s+[\w-]+\s*\(([^)]*)\)", content):
        for pm in re.finditer(r"\$([A-Za-z_]\w*)", fm.group(1)):
            assigned.add(pm.group(1))

    undefined = sorted(refs - assigned - skip)
    assert not undefined, f"Undefined PowerShell vars in install.ps1: {undefined}"


# ─── 3. install.sh — no undefined bash variables ────────────────────

def test_install_sh_no_undefined_vars():
    """Every $VAR referenced in install.sh must be assigned or a known environment."""
    content = (ROOT / "install.sh").read_text(encoding="utf-8")

    skip = {
        "HOME", "PATH", "PROFILE", "LLM_WIKI_ROOT", "LLM_WIKI_STATE_ROOT",
        "XDG_CONFIG_HOME",
        # Standard bash/environment builtins not assigned inside the script.
        "SHELL", "BASH_SOURCE", "ZSH_VERSION", "BASH_VERSION",
    }

    # Collect all $VAR and ${VAR} references (not $(...) command subs).
    refs: set[str] = set()
    for m in re.finditer(r"\$\{?([A-Za-z_]\w*)", content):
        var = m.group(1)
        if var in skip:
            continue
        refs.add(var)

    # Collect assignments: VAR= or export VAR=
    assigned: set[str] = set()
    for m in re.finditer(
        r"(?:^|\s|;)(?:export\s+)?([A-Za-z_]\w*)\s*=", content, re.MULTILINE
    ):
        assigned.add(m.group(1))

    undefined = sorted(refs - assigned - skip)
    assert not undefined, f"Undefined bash vars in install.sh: {undefined}"


def test_installers_use_effective_xdg_opencode_plugin_destination():
    """Installers must target OpenCode's effective XDG config directory."""
    install_ps1 = (ROOT / "install.ps1").read_text(encoding="utf-8")
    install_sh = (ROOT / "install.sh").read_text(encoding="utf-8")

    assert "function Get-OpenCodeConfigs" in install_ps1
    assert "[System.IO.Path]::IsPathFullyQualified($env:XDG_CONFIG_HOME)" in install_ps1
    assert "$openCodeConfigs = @(Get-OpenCodeConfigs)" in install_ps1
    assert "resolve_opencode_config_home()" in install_sh
    assert 'OPENCODE_CONFIG_HOME="$(resolve_opencode_config_home)/opencode"' in install_sh


def test_windows_task_installer_registers_windowless_python(tmp_path):
    pwsh = shutil.which("pwsh")
    if os.name != "nt" or not pwsh:
        pytest.skip("Windows PowerShell 7 is unavailable")

    vault = tmp_path / "vault with spaces"
    scripts = vault / "scripts"
    venv_scripts = vault / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    venv_scripts.mkdir(parents=True)
    for name in ("scheduled_nightly.py", "scheduled_weekly.py"):
        (scripts / name).write_text("", encoding="utf-8")
    for name in ("python.exe", "pythonw.exe"):
        (venv_scripts / name).write_bytes(b"")

    harness = r"""
$ErrorActionPreference = 'Stop'
$script:Registrations = @()
function New-ScheduledTaskAction {
    param([string]$Execute, [string]$Argument, [string]$WorkingDirectory)
    [pscustomobject]@{
        Execute = $Execute
        Argument = $Argument
        WorkingDirectory = $WorkingDirectory
    }
}
function New-ScheduledTaskTrigger {
    param(
        [switch]$Daily,
        [switch]$Weekly,
        [datetime]$At,
        [string[]]$DaysOfWeek
    )
    [pscustomobject]@{ Daily = $Daily; Weekly = $Weekly; At = $At }
}
function New-ScheduledTaskSettingsSet {
    param(
        [switch]$AllowStartIfOnBatteries,
        [switch]$DontStopIfGoingOnBatteries,
        [switch]$StartWhenAvailable,
        [timespan]$ExecutionTimeLimit,
        [int]$RestartCount,
        [timespan]$RestartInterval
    )
    [pscustomobject]@{ RestartCount = $RestartCount }
}
function New-ScheduledTaskPrincipal {
    param([string]$UserId, [string]$LogonType, [string]$RunLevel)
    [pscustomobject]@{ UserId = $UserId; LogonType = $LogonType; RunLevel = $RunLevel }
}
function Unregister-ScheduledTask {
    param(
        [string]$TaskName,
        [switch]$Confirm,
        [object]$ErrorAction
    )
}
function Register-ScheduledTask {
    param(
        [string]$TaskName,
        $Action,
        $Trigger,
        $Settings,
        $Principal,
        [string]$Description,
        [switch]$Force
    )
    $script:Registrations += [pscustomobject]@{
        TaskName = $TaskName
        Action = $Action
        Principal = $Principal
    }
}
. $env:LLM_WIKI_TASK_INSTALLER
'LLM_WIKI_JSON:' + ($script:Registrations | ConvertTo-Json -Compress -Depth 6)
"""
    result = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-Command", harness],
        env={
            **os.environ,
            "LLM_WIKI_ROOT": str(vault),
            "LLM_WIKI_STATE_ROOT": str(vault),
            "LLM_WIKI_TASK_INSTALLER": str(
                ROOT / "scripts" / "install-scheduled-tasks.ps1"
            ),
        },
        check=True,
        capture_output=True,
        text=True,
    )
    payload = next(
        line.removeprefix("LLM_WIKI_JSON:")
        for line in result.stdout.splitlines()
        if line.startswith("LLM_WIKI_JSON:")
    )
    registrations = json.loads(payload)
    if isinstance(registrations, dict):
        registrations = [registrations]

    assert {item["TaskName"] for item in registrations} == {
        "LLMWiki-Nightly",
        "LLMWiki-Weekly",
    }
    assert {
        Path(item["Action"]["Execute"]).name.casefold() for item in registrations
    } == {"pythonw.exe"}
    assert all(
        Path(item["Action"]["WorkingDirectory"]).resolve() == vault.resolve()
        for item in registrations
    )


def test_windows_maintenance_steps_never_create_console(monkeypatch):
    import maintenance_helpers

    observed = {}

    def fake_run(cmd, **kwargs):
        observed.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(maintenance_helpers, "_WINDOWS", True, raising=False)
    monkeypatch.setattr(maintenance_helpers.subprocess, "run", fake_run)

    assert maintenance_helpers.run_step(["python", "child.py"], lambda _line: None, "child") == 0
    assert observed["creationflags"] & 0x08000000


def test_windowless_maintenance_logging_tolerates_missing_console(monkeypatch):
    import maintenance_helpers

    monkeypatch.setattr(
        maintenance_helpers,
        "sys",
        SimpleNamespace(stdout=None, stderr=None),
        raising=False,
    )

    maintenance_helpers.write_console("stdout message")
    maintenance_helpers.write_console("stderr message", error=True)


@pytest.mark.parametrize("installer", ["install.ps1", "install.sh"])
def test_installer_preflight_has_no_persistent_configuration_mutation(installer):
    source = (ROOT / installer).read_text(encoding="utf-8")
    success_marker = 'Ok $testResult' if installer.endswith(".ps1") else 'ok "All tests passed"'
    gate_end = source.index(success_marker) + len(success_marker)
    prefix = source[:gate_end]

    forbidden = (
        "remote set-url --push",
        "SetEnvironmentVariable",
        "Add-Content",
        "Copy-Item",
        "crontab",
        "PLUGIN_DIR=",
    )
    assert not [token for token in forbidden if token in prefix]


def test_stdin_shell_installer_never_uses_ambient_project_as_vault(tmp_path):
    bash = _find_bash()
    if not bash:
        pytest.skip("POSIX-compatible bash is unavailable")

    ambient = tmp_path / "unrelated fake project"
    home = tmp_path / "home"
    fake_bin = tmp_path / "fake-bin"
    _init_installer_vault(ambient, project_name="unrelated-project")
    home.mkdir()
    fake_bin.mkdir()
    uv_log = tmp_path / "uv.log"
    git_log = tmp_path / "git.log"
    (fake_bin / "python3").write_text("#!/bin/sh\necho 3.10\n", encoding="utf-8")
    (fake_bin / "uv").write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$UV_LOG\"\n"
        "[ \"$1\" = --version ] && echo 'uv 0-test'\n"
        "exit 0\n",
        encoding="utf-8",
    )
    (fake_bin / "git").write_text(
        "#!/bin/sh\n"
        "first=${1-}\n"
        "printf '%s' \"$first\" >> \"$GIT_LOG\"\n"
        "shift || true\n"
        "for arg in \"$@\"; do printf '\\t%s' \"$arg\" >> \"$GIT_LOG\"; done\n"
        "printf '\\n' >> \"$GIT_LOG\"\n"
        "[ \"$first\" = --version ] && { echo 'git version 0-test'; exit 0; }\n"
        "[ \"$first\" = clone ] && exit 23\n"
        "exit 29\n",
        encoding="utf-8",
    )
    for name in ("python3", "uv", "git"):
        (fake_bin / name).chmod(0o755)
    env = os.environ.copy()
    env.pop("LLM_WIKI_ROOT", None)
    env.pop("LLM_WIKI_STATE_ROOT", None)
    env.update(
        {
            "HOME": _path_for_bash(bash, home),
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "UV_LOG": _path_for_bash(bash, uv_log),
            "GIT_LOG": _path_for_bash(bash, git_log),
        }
    )

    result = subprocess.run(
        [
            bash,
            "-c",
            'PATH="$1:$PATH"; export PATH; exec bash -s',
            "--",
            _path_for_bash(bash, fake_bin),
        ],
        cwd=ambient,
        env=env,
        input=(ROOT / "install.sh").read_bytes(),
        capture_output=True,
    )

    assert result.returncode != 0
    assert _push_url(ambient) == "https://example.invalid/push"
    assert not uv_log.exists(), result.stdout.decode(errors="replace")
    calls = git_log.read_text(encoding="utf-8")
    ambient_for_bash = _path_for_bash(bash, ambient)
    assert f"-C\t{ambient_for_bash}\tremote\tset-url\t--push\torigin\tno-push" not in calls
    assert not (home / ".bashrc").exists()
    assert not (ambient / "run").exists()


def test_stdin_shell_installer_accepts_valid_explicit_vault_not_ambient_pwd(tmp_path):
    bash = _find_bash()
    if not bash:
        pytest.skip("POSIX-compatible bash is unavailable")
    ambient = tmp_path / "unrelated project"
    vault = tmp_path / "explicit llm wiki"
    home = tmp_path / "home"
    fake_bin = tmp_path / "fake-bin"
    _init_installer_vault(ambient, project_name="unrelated-project")
    _init_installer_vault(vault)
    home.mkdir()
    _write_successful_installer_stubs(fake_bin)
    uv_log = tmp_path / "uv.log"
    crontab_store = tmp_path / "crontab"
    expected = _path_for_bash(bash, vault)
    env = {
        **os.environ,
        "HOME": _path_for_bash(bash, home),
        "SHELL": "/bin/bash",
        "LLM_WIKI_ROOT": expected,
        "UV_LOG": _path_for_bash(bash, uv_log),
        "CRONTAB_STORE": _path_for_bash(bash, crontab_store),
    }

    result = subprocess.run(
        [
            bash,
            "-c",
            'PATH="$1:$PATH"; export PATH; exec bash -s',
            "--",
            _path_for_bash(bash, fake_bin),
        ],
        cwd=ambient,
        env=env,
        input=(ROOT / "install.sh").read_bytes(),
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert f"Vault root: {expected}" in result.stdout.decode(errors="replace")
    assert (vault / "run" / "queue").is_dir()
    assert not (ambient / "run").exists()
    assert _push_url(ambient) == "https://example.invalid/push"


def test_shell_installer_with_unset_shell_uses_bash_fallback_profile(tmp_path):
    bash = _find_bash()
    if not bash:
        pytest.skip("POSIX-compatible bash is unavailable")
    vault = tmp_path / "valid checkout"
    home = tmp_path / "home"
    fake_bin = tmp_path / "fake-bin"
    _init_installer_vault(vault)
    shutil.copy2(ROOT / "install.sh", vault / "install.sh")
    home.mkdir()
    _write_successful_installer_stubs(fake_bin)
    uv_log = tmp_path / "uv.log"
    crontab_store = tmp_path / "crontab"
    env = {
        **os.environ,
        "HOME": _path_for_bash(bash, home),
        "UV_LOG": _path_for_bash(bash, uv_log),
        "CRONTAB_STORE": _path_for_bash(bash, crontab_store),
    }
    env.pop("SHELL", None)

    result = subprocess.run(
        [
            bash,
            "-c",
            'unset SHELL; PATH="$1:$PATH"; export PATH; . "$2"',
            "--",
            _path_for_bash(bash, fake_bin),
            _path_for_bash(bash, vault / "install.sh"),
        ],
        env=env,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert (home / ".bashrc").is_file()
    assert not (home / ".profile").exists()
    assert (vault / "run" / "queue").is_dir()
    assert crontab_store.is_file()


@pytest.mark.parametrize("fail_at", ["sync", "pytest"])
def test_failed_shell_install_leaves_remote_and_config_untouched(tmp_path, fail_at):
    bash = _find_bash()
    if not bash:
        pytest.skip("POSIX-compatible bash is unavailable")

    vault = tmp_path / "vault with spaces"
    home = tmp_path / "home's config"
    fake_bin = tmp_path / "fake-bin"
    _init_installer_vault(vault)
    fake_bin.mkdir()
    home.mkdir()
    sentinel = home / ".config" / "opencode" / "sentinel"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("unchanged", encoding="utf-8")
    (fake_bin / "python3").write_text("#!/bin/sh\necho 3.10\n", encoding="utf-8")
    (fake_bin / "uv").write_text(
        "#!/bin/sh\n"
        "[ \"$1\" = --version ] && { echo 'uv 0-test'; exit 0; }\n"
        "[ \"$1\" = sync ] && [ \"$UV_FAIL_AT\" = sync ] && exit 17\n"
        "[ \"$1 $2 $3\" = 'run pytest -q' ] && [ \"$UV_FAIL_AT\" = pytest ] && exit 19\n"
        "exit 0\n",
        encoding="utf-8",
    )
    (fake_bin / "python3").chmod(0o755)
    (fake_bin / "uv").chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "LLM_WIKI_ROOT": str(vault),
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "UV_FAIL_AT": fail_at,
        }
    )

    result = subprocess.run([bash, str(ROOT / "install.sh")], env=env, capture_output=True)

    assert result.returncode != 0
    assert _push_url(vault) == "https://example.invalid/push"
    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    assert not (home / ".bashrc").exists()


@pytest.mark.parametrize("fail_at", ["sync", "pytest"])
def test_failed_powershell_install_leaves_remote_and_config_untouched(tmp_path, fail_at):
    pwsh = shutil.which("pwsh")
    if os.name != "nt" or not pwsh:
        pytest.skip("Windows PowerShell 7 is unavailable")

    vault = tmp_path / "vault with spaces"
    home = tmp_path / "home's config"
    fake_bin = tmp_path / "fake-bin"
    _init_installer_vault(vault)
    fake_bin.mkdir()
    home.mkdir()
    sentinel = home / ".config" / "opencode" / "sentinel"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("unchanged", encoding="utf-8")
    (fake_bin / "python.cmd").write_text("@echo Python 3.10.0\r\n", encoding="ascii")
    (fake_bin / "uv.cmd").write_text(
        "@echo off\r\n"
        "if \"%1\"==\"--version\" (echo uv 0-test& exit /b 0)\r\n"
        "if \"%1\"==\"sync\" if \"%UV_FAIL_AT%\"==\"sync\" exit /b 17\r\n"
        "if \"%1 %2 %3\"==\"run pytest -q\" if \"%UV_FAIL_AT%\"==\"pytest\" exit /b 19\r\n"
        "exit /b 0\r\n",
        encoding="ascii",
    )
    env = os.environ.copy()
    env.update(
        {
            "USERPROFILE": str(home),
            "LLM_WIKI_ROOT": str(vault),
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "UV_FAIL_AT": fail_at,
        }
    )

    result = subprocess.run(
        [pwsh, "-NoProfile", "-File", str(ROOT / "install.ps1")],
        env=env,
        capture_output=True,
    )

    assert result.returncode != 0
    assert _push_url(vault) == "https://example.invalid/push"
    assert sentinel.read_text(encoding="utf-8") == "unchanged"


def test_powershell_reinstall_prefers_invoked_checkout_over_stale_env_root(tmp_path):
    pwsh = shutil.which("pwsh")
    if os.name != "nt" or not pwsh:
        pytest.skip("Windows PowerShell 7 is unavailable")

    old_root = tmp_path / "old installed checkout"
    new_root = tmp_path / "new invoked checkout"
    home = tmp_path / "isolated home"
    fake_bin = tmp_path / "fake-bin"
    uv_log = tmp_path / "uv.log"
    task_log = tmp_path / "task.log"
    old_task_marker = tmp_path / "old-task-invoked"
    for root in (old_root, new_root):
        _init_installer_vault(root)
        (root / "scripts").mkdir()
    home.mkdir()
    fake_bin.mkdir()
    (home / ".config" / "opencode").mkdir(parents=True)
    (home / ".claude").mkdir()
    (old_root / "scripts" / "llm-wiki-memory-opencode.js").write_text(
        "OLD_PLUGIN_MUST_NOT_BE_INSTALLED\n",
        encoding="utf-8",
    )
    (new_root / "scripts" / "llm-wiki-memory-opencode.js").write_text(
        "NEW_PLUGIN_SELECTED\n",
        encoding="utf-8",
    )
    (old_root / "scripts" / "install-scheduled-tasks.ps1").write_text(
        f"Set-Content -LiteralPath '{old_task_marker}' -Value invoked\n",
        encoding="utf-8",
    )
    (new_root / "scripts" / "install-scheduled-tasks.ps1").write_text(
        "Set-Content -LiteralPath $env:TASK_LOG -Value $PSScriptRoot -NoNewline\n"
        "$global:LASTEXITCODE = 0\n",
        encoding="utf-8",
    )
    source = (ROOT / "install.ps1").read_text(encoding="utf-8")
    for name in ("LLM_WIKI_ROOT", "LLM_WIKI_STATE_ROOT", "MEMORY_LLM_PROVIDER"):
        value = "$VAULT_ROOT" if name != "MEMORY_LLM_PROVIDER" else '"opencode-sdk"'
        source = source.replace(
            f'[Environment]::SetEnvironmentVariable("{name}", {value}, "User")',
            f'[Environment]::SetEnvironmentVariable("{name}", {value}, "Process")',
        )
    assert source.count('SetEnvironmentVariable("LLM_WIKI_', 0, len(source)) == 2
    assert 'SetEnvironmentVariable("MEMORY_LLM_PROVIDER", "opencode-sdk", "User")' not in source
    (new_root / "install.ps1").write_text(source, encoding="utf-8")
    (fake_bin / "python.cmd").write_text(
        "@echo Python 3.10.0\r\n",
        encoding="ascii",
    )
    (fake_bin / "uv.cmd").write_text(
        "@echo off\r\n"
        ">>\"%UV_LOG%\" echo %CD%^|%*\r\n"
        "exit /b 0\r\n",
        encoding="ascii",
    )
    (fake_bin / "git.cmd").write_text("@exit /b 0\r\n", encoding="ascii")
    env = {
        **os.environ,
        "USERPROFILE": str(home),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "LLM_WIKI_ROOT": str(old_root),
        "LLM_WIKI_STATE_ROOT": str(old_root),
        "PATH": str(fake_bin),
        "UV_LOG": str(uv_log),
        "TASK_LOG": str(task_log),
    }

    result = subprocess.run(
        [pwsh, "-NoProfile", "-File", str(new_root / "install.ps1")],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert f"Vault root: {new_root}" in result.stdout
    calls = uv_log.read_text(encoding="utf-8").splitlines()
    assert any(call.endswith("|run pytest -q") for call in calls)
    assert any("merge_claude_settings.py" in call for call in calls)
    assert all(
        Path(call.split("|", 1)[0]).resolve() == new_root.resolve()
        for call in calls
    )
    assert str(old_root).casefold() not in "\n".join(calls).casefold()
    installed_plugin = home / ".config" / "opencode" / "plugins" / "llm-wiki-memory.js"
    assert installed_plugin.read_text(encoding="utf-8") == "NEW_PLUGIN_SELECTED\n"
    assert Path(task_log.read_text(encoding="utf-8")).resolve() == (
        new_root / "scripts"
    ).resolve()
    assert not old_task_marker.exists()
    assert (new_root / "run" / "queue").is_dir()
    assert not (old_root / "run").exists()


@pytest.mark.parametrize(
    "xdg",
    [None, "", ("relative/config", "C:/relative", "C:\\relative")],
    ids=("unset", "empty", "relative"),
)
def test_shell_xdg_resolver_ignores_empty_and_relative_values(tmp_path, xdg):
    bash = _find_bash()
    if not bash:
        pytest.skip("POSIX-compatible bash is unavailable")
    source = (ROOT / "install.sh").read_text(encoding="utf-8")
    function = _extract_braced_function(source, "resolve_opencode_config_home()")
    home = tmp_path / "home's folder"
    bash_home = _path_for_bash(bash, home)
    env = os.environ.copy()
    env["HOME"] = bash_home
    candidates = xdg if isinstance(xdg, tuple) else (xdg,)
    for candidate in candidates:
        if candidate is None:
            env.pop("XDG_CONFIG_HOME", None)
        else:
            env["XDG_CONFIG_HOME"] = candidate

        result = subprocess.run(
            [bash, "-c", f'{function}\nprintf "%s" "$(resolve_opencode_config_home)"'],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

        assert result.stdout == f"{bash_home}/.config"


def test_shell_xdg_resolver_preserves_absolute_path_with_spaces_and_apostrophe(tmp_path):
    bash = _find_bash()
    if not bash:
        pytest.skip("POSIX-compatible bash is unavailable")
    source = (ROOT / "install.sh").read_text(encoding="utf-8")
    function = _extract_braced_function(source, "resolve_opencode_config_home()")
    xdg = tmp_path / "XDG's config dir"
    bash_xdg = _path_for_bash(bash, xdg)
    env = {
        **os.environ,
        "HOME": _path_for_bash(bash, tmp_path / "home"),
        "XDG_CONFIG_HOME": bash_xdg,
    }

    result = subprocess.run(
        [bash, "-c", f'{function}\nprintf "%s" "$(resolve_opencode_config_home)"'],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout == bash_xdg


@pytest.mark.parametrize(
    "path",
    [
        "/tmp/vault's dir",
        "/tmp/vault ;$(printf injected)& [dir]",
    ],
)
def test_shell_quote_round_trips_arbitrary_path(path):
    bash = _find_bash()
    if not bash:
        pytest.skip("POSIX-compatible bash is unavailable")
    source = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "shell_quote()" in source
    function = _extract_braced_function(source, "shell_quote()")
    quoted = subprocess.run(
        [bash, "-c", f'{function}\nshell_quote "$1"', "--", path],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    result = subprocess.run(
        [bash, "-c", 'eval "set -- $1"; [ "$#" -eq 1 ]; printf "%s" "$1"', "--", quoted],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout == path


def test_shell_profile_exports_round_trip_without_expansion(tmp_path):
    bash = _find_bash()
    if not bash:
        pytest.skip("POSIX-compatible bash is unavailable")
    profile_path = tmp_path / "profile"
    profile = _path_for_bash(bash, profile_path)
    sentinel = _path_for_bash(bash, tmp_path / "command-substitution-ran")
    backtick_sentinel = _path_for_bash(bash, tmp_path / "backtick-ran")
    value = (
        "/tmp/vault's dir "
        '$(printf command-substitution >"$SENTINEL") '
        '`printf backtick >"$BACKTICK_SENTINEL"` '
        r"\literal\path"
        "\nsecond line"
    )
    _run_shell_profile_update(bash, profile_path, value)
    result = subprocess.run(
        [
            bash,
            "-c",
            r'set -eu; unset LLM_WIKI_ROOT LLM_WIKI_STATE_ROOT; . "$1"; '
            r'printf "%s\0%s\0" "$LLM_WIKI_ROOT" "$LLM_WIKI_STATE_ROOT"',
            "--",
            profile,
        ],
        env={
            **os.environ,
            "SENTINEL": sentinel,
            "BACKTICK_SENTINEL": backtick_sentinel,
        },
        check=True,
        capture_output=True,
    )

    assert result.stdout == value.encode() + b"\0" + value.encode() + b"\0"
    assert not Path(tmp_path / "command-substitution-ran").exists()
    assert not Path(tmp_path / "backtick-ran").exists()


def test_shell_profile_update_replaces_only_exact_exports_on_successive_runs(tmp_path):
    bash = _find_bash()
    if not bash:
        pytest.skip("POSIX-compatible bash is unavailable")
    profile = tmp_path / "profile"
    unrelated = (
        "# user's profile with O'Brien and $(printf untouched)\n"
        "export PATH='/custom/bin':\"$PATH\"\n"
        "# export LLM_WIKI_ROOT='/comment-decoy'\n"
        "export NOT_LLM_WIKI_ROOT='/prefix-decoy'\n"
        "export LLM_WIKI_ROOT_SUFFIX='/suffix-decoy'\n"
        "LLM_WIKI_STATE_ROOT='/unexported-decoy'\n"
        "  export MEMORY_LLM_PROVIDER='indented-decoy'\n"
    )
    profile.write_text(
        unrelated
        + "export LLM_WIKI_ROOT='/stale root'\n"
        + "export LLM_WIKI_STATE_ROOT='/stale state'\n"
        + "export MEMORY_LLM_PROVIDER='stale-provider'\n"
        + "alias keep='printf \\\"%s\\\\n\\\" safe'\n",
        encoding="utf-8",
    )
    profile.chmod(0o640)
    original_mode = stat.S_IMODE(profile.stat().st_mode)
    first = "/tmp/first O'Brien;$(printf inert)&[vault]"
    latest = "/tmp/latest O'Brien;`printf inert`&[vault]"

    _run_shell_profile_update(bash, profile, first)
    _run_shell_profile_update(bash, profile, latest)

    text = profile.read_text(encoding="utf-8")
    assert text.startswith(unrelated)
    assert "alias keep='printf \\\"%s\\\\n\\\" safe'\n" in text
    for name in ("LLM_WIKI_ROOT", "LLM_WIKI_STATE_ROOT", "MEMORY_LLM_PROVIDER"):
        assert len(re.findall(rf"^export {name}=", text, re.MULTILINE)) == 1
    assert first not in text
    assert stat.S_IMODE(profile.stat().st_mode) == original_mode
    assert not list(tmp_path.glob("profile.llm-wiki.tmp.*"))

    result = subprocess.run(
        [
            bash,
            "-c",
            'set -eu; unset LLM_WIKI_ROOT LLM_WIKI_STATE_ROOT MEMORY_LLM_PROVIDER; '
            '. "$1"; printf "%s\\0%s\\0%s\\0" "$LLM_WIKI_ROOT" '
            '"$LLM_WIKI_STATE_ROOT" "$MEMORY_LLM_PROVIDER"',
            "--",
            _path_for_bash(bash, profile),
        ],
        check=True,
        capture_output=True,
    )
    assert result.stdout == (
        latest.encode() + b"\0" + latest.encode() + b"\0opencode-sdk\0"
    )


def test_shell_profile_update_preserves_symlink_and_updates_referent(tmp_path):
    bash = _find_bash()
    if not bash:
        pytest.skip("POSIX-compatible bash is unavailable")
    referent_dir = tmp_path / "profile storage"
    link_dir = tmp_path / "home"
    referent_dir.mkdir()
    link_dir.mkdir()
    referent = referent_dir / "actual-profile"
    original = "# keep\nexport LLM_WIKI_ROOT='/old'\n"
    referent.write_text(original, encoding="utf-8")
    referent.chmod(0o640)
    expected_mode = stat.S_IMODE(referent.stat().st_mode)
    profile = link_dir / ".bashrc"
    try:
        profile.symlink_to(referent)
    except OSError as exc:
        pytest.skip(f"profile symlink creation is unavailable: {exc}")

    _run_shell_profile_update(bash, profile, "/tmp/new vault")

    assert profile.is_symlink()
    assert profile.resolve() == referent.resolve()
    text = referent.read_text(encoding="utf-8")
    assert text.startswith("# keep\n")
    assert "export LLM_WIKI_ROOT='/tmp/new vault'\n" in text
    assert "export LLM_WIKI_ROOT='/old'\n" not in text
    assert stat.S_IMODE(referent.stat().st_mode) == expected_mode
    assert not list(referent_dir.glob(".*.llm-wiki.*"))
    assert not list(link_dir.glob(".*.llm-wiki.*"))


def test_shell_profile_update_aborts_if_symlink_is_retargeted_before_replace(tmp_path):
    bash = _find_bash()
    if not bash:
        pytest.skip("POSIX-compatible bash is unavailable")
    referent_dir = tmp_path / "profile storage"
    link_dir = tmp_path / "home"
    referent_dir.mkdir()
    link_dir.mkdir()
    original_referent = referent_dir / "original-profile"
    replacement_referent = referent_dir / "replacement-profile"
    original_bytes = b"# original referent\nexport LLM_WIKI_ROOT='/old'\n"
    replacement_bytes = b"# replacement referent\n"
    original_referent.write_bytes(original_bytes)
    replacement_referent.write_bytes(replacement_bytes)
    profile = link_dir / ".bashrc"
    try:
        profile.symlink_to(original_referent)
    except OSError as exc:
        pytest.skip(f"profile symlink creation is unavailable: {exc}")
    source = (ROOT / "install.sh").read_text(encoding="utf-8")
    update_function = _extract_braced_function(source, "update_shell_profile()")
    command = (
        f"set -euo pipefail\n{update_function}\n"
        "profile_path=$1\n"
        "replacement_referent=$2\n"
        "shell_quote() {\n"
        "  ln -sfn \"$replacement_referent\" \"$profile_path\"\n"
        "  printf \"'%s'\" \"$1\"\n"
        "}\n"
        'update_shell_profile "$1" "/tmp/new" "/tmp/new" "opencode-sdk"'
    )

    result = subprocess.run(
        [
            bash,
            "-c",
            command,
            "--",
            _path_for_bash(bash, profile),
            _path_for_bash(bash, replacement_referent),
        ],
        capture_output=True,
    )

    assert result.returncode != 0
    assert profile.is_symlink()
    assert profile.resolve() == replacement_referent.resolve()
    assert original_referent.read_bytes() == original_bytes
    assert replacement_referent.read_bytes() == replacement_bytes
    assert not list(referent_dir.glob(".*.llm-wiki.*"))
    assert not list(link_dir.glob(".*.llm-wiki.*"))


def test_shell_profile_update_aborts_on_external_change_before_replace(tmp_path):
    bash = _find_bash()
    if not bash:
        pytest.skip("POSIX-compatible bash is unavailable")
    profile = tmp_path / "profile"
    profile.write_bytes(b"# merge base\nexport LLM_WIKI_ROOT='/old'\n")
    source = (ROOT / "install.sh").read_text(encoding="utf-8")
    quote_function = _extract_braced_function(source, "shell_quote()")
    update_function = _extract_braced_function(source, "update_shell_profile()")
    external = b"# external editor wins\n"
    command = (
        f"set -euo pipefail\n{quote_function}\n{update_function}\n"
        'PROFILE_TARGET="$1"\n'
        "export PROFILE_TARGET\n"
        "cmp() {\n"
        "  printf '# external editor wins\\n' > \"$PROFILE_TARGET\"\n"
        "  command cmp \"$@\"\n"
        "}\n"
        'update_shell_profile "$1" "/tmp/new" "/tmp/new" "opencode-sdk"'
    )

    result = subprocess.run(
        [bash, "-c", command, "--", _path_for_bash(bash, profile)],
        capture_output=True,
    )

    assert result.returncode != 0
    assert profile.read_bytes() == external
    assert not list(tmp_path.glob(".*.llm-wiki.*"))
    assert not list(tmp_path.glob("profile.llm-wiki.*"))


def test_successive_shell_installs_prefer_checkout_and_refresh_process_environment(
    tmp_path,
):
    bash = _find_bash()
    if not bash:
        pytest.skip("POSIX-compatible bash is unavailable")
    home = tmp_path / "home's config"
    fake_bin = tmp_path / "fake-bin"
    stale_state = tmp_path / "stale state"
    first_vault = tmp_path / "first checkout"
    latest_vault = tmp_path / "latest O'Brien [checkout]"
    home.mkdir()
    _write_successful_installer_stubs(fake_bin)
    (fake_bin / "codex").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (fake_bin / "codex").chmod(0o755)
    _init_installer_vault(first_vault)
    _init_installer_vault(latest_vault)
    shutil.copy2(ROOT / "install.sh", first_vault / "install.sh")
    shutil.copy2(ROOT / "install.sh", latest_vault / "install.sh")
    profile = home / ".bashrc"
    decoys = (
        "# keep-before\n"
        "# export LLM_WIKI_ROOT='/comment'\n"
        "export MY_LLM_WIKI_ROOT='/similar'\n"
        "export LLM_WIKI_ROOT_EXTRA='/similar-suffix'\n"
        "printf '%s\\n' \"O'Brien; $(printf literal)\" >/dev/null\n"
        "export LLM_WIKI_ROOT='/very-old'\n"
        "export LLM_WIKI_STATE_ROOT='/very-old-state'\n"
        "export MEMORY_LLM_PROVIDER='very-old-provider'\n"
        "# keep-after\n"
    )
    profile.write_text(decoys, encoding="utf-8")
    profile.chmod(0o640)
    original_mode = stat.S_IMODE(profile.stat().st_mode)
    uv_log = tmp_path / "uv.log"
    crontab_store = tmp_path / "crontab"
    env = {
        **os.environ,
        "HOME": _path_for_bash(bash, home),
        "SHELL": "/bin/bash",
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "UV_LOG": _path_for_bash(bash, uv_log),
        "CRONTAB_STORE": _path_for_bash(bash, crontab_store),
        "LLM_WIKI_ROOT": _path_for_bash(bash, first_vault),
        "LLM_WIKI_STATE_ROOT": _path_for_bash(bash, stale_state),
        "MEMORY_LLM_PROVIDER": "stale-process-provider",
    }

    for vault in (first_vault, latest_vault):
        result = subprocess.run(
            [
                bash,
                "-c",
                'PATH="$1:$PATH"; export PATH; "$2"',
                "--",
                _path_for_bash(bash, fake_bin),
                _path_for_bash(bash, vault / "install.sh"),
            ],
            env=env,
            check=True,
            capture_output=True,
        )
        expected = _path_for_bash(bash, vault)
        assert f"Vault root: {expected}" in result.stdout.decode(errors="replace")
        env["LLM_WIKI_ROOT"] = expected

    latest = _path_for_bash(bash, latest_vault)
    text = profile.read_text(encoding="utf-8")
    for line in (
        "# keep-before\n",
        "# export LLM_WIKI_ROOT='/comment'\n",
        "export MY_LLM_WIKI_ROOT='/similar'\n",
        "export LLM_WIKI_ROOT_EXTRA='/similar-suffix'\n",
        "printf '%s\\n' \"O'Brien; $(printf literal)\" >/dev/null\n",
        "# keep-after\n",
    ):
        assert line in text
    for name in ("LLM_WIKI_ROOT", "LLM_WIKI_STATE_ROOT", "MEMORY_LLM_PROVIDER"):
        assert len(re.findall(rf"^export {name}=", text, re.MULTILINE)) == 1
    assert stat.S_IMODE(profile.stat().st_mode) == original_mode
    assert not list(home.glob(".bashrc.llm-wiki.tmp.*"))
    assert (latest_vault / "run" / "queue").is_dir()
    assert (latest_vault / "logs").is_dir()
    assert (latest_vault / "cache" / "cognee").is_dir()
    assert not stale_state.exists()
    quote_function = _extract_braced_function(
        (ROOT / "install.sh").read_text(encoding="utf-8"), "shell_quote()"
    )
    quoted_latest = subprocess.run(
        [bash, "-c", f'{quote_function}\nshell_quote "$1"', "--", latest],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert quoted_latest in crontab_store.read_text(encoding="utf-8")

    sourced = subprocess.run(
        [
            bash,
            "-c",
            'set -eu; unset LLM_WIKI_ROOT LLM_WIKI_STATE_ROOT MEMORY_LLM_PROVIDER; '
            '. "$1"; printf "%s\\0%s\\0%s\\0" "$LLM_WIKI_ROOT" '
            '"$LLM_WIKI_STATE_ROOT" "$MEMORY_LLM_PROVIDER"',
            "--",
            _path_for_bash(bash, profile),
        ],
        check=True,
        capture_output=True,
    )
    assert sourced.stdout == (
        latest.encode() + b"\0" + latest.encode() + b"\0opencode-sdk\0"
    )
    post_config = [
        line.split("\t", 2)
        for line in uv_log.read_text(encoding="utf-8").splitlines()
        if "merge_" in line or "session_start_context.py" in line
    ]
    first = _path_for_bash(bash, first_vault)
    assert post_config
    assert any("merge_codex_hooks.py" in parts[2] for parts in post_config)
    assert all(parts[:2] in ([first, first], [latest, latest]) for parts in post_config)
    assert any(parts[:2] == [latest, latest] for parts in post_config)


def test_cron_lines_use_shell_quoted_vault_state_uv_and_log_paths():
    source = (ROOT / "install.sh").read_text(encoding="utf-8")

    for quoted_name in (
        "VAULT_ROOT_Q",
        "STATE_ROOT_Q",
        "UV_BIN_Q",
        "NIGHTLY_LOG_Q",
        "WEEKLY_LOG_Q",
    ):
        assert f'{quoted_name}=$(cron_quote "' in source
    cron_block = source[source.index('CRON_ENV='):source.index("# Remove old LLM-Wiki cron block")]
    assert "cd $VAULT_ROOT_Q" in cron_block
    assert "LLM_WIKI_ROOT=$VAULT_ROOT_Q" in cron_block
    assert "LLM_WIKI_STATE_ROOT=$STATE_ROOT_Q" in cron_block
    assert "$UV_BIN_Q run python" in cron_block
    assert ">> $NIGHTLY_LOG_Q" in cron_block
    assert ">> $WEEKLY_LOG_Q" in cron_block


def test_cron_percent_paths_survive_crontab_parsing_and_reach_shell(tmp_path):
    bash = _find_bash()
    if not bash:
        pytest.skip("POSIX-compatible bash is unavailable")
    vault = tmp_path / "100%wiki checkout"
    home = tmp_path / "home"
    fake_bin = tmp_path / "fake-bin"
    _init_installer_vault(vault)
    shutil.copy2(ROOT / "install.sh", vault / "install.sh")
    home.mkdir()
    _write_successful_installer_stubs(fake_bin)
    uv_log = tmp_path / "uv.log"
    crontab_store = tmp_path / "crontab"
    env = {
        **os.environ,
        "HOME": _path_for_bash(bash, home),
        "SHELL": "/bin/bash",
        "UV_LOG": _path_for_bash(bash, uv_log),
        "CRONTAB_STORE": _path_for_bash(bash, crontab_store),
    }

    subprocess.run(
        [
            bash,
            "-c",
            'PATH="$1:$PATH"; export PATH; "$2"',
            "--",
            _path_for_bash(bash, fake_bin),
            _path_for_bash(bash, vault / "install.sh"),
        ],
        env=env,
        check=True,
        capture_output=True,
    )
    uv_log.unlink()
    entries = [
        line
        for line in crontab_store.read_text(encoding="utf-8").splitlines()
        if line.startswith(("0 3 ", "0 4 "))
    ]
    assert len(entries) == 2

    for entry in entries:
        raw_command = entry.split(maxsplit=5)[5]
        shell_command, cron_stdin = _simulate_crontab_command(raw_command)
        assert cron_stdin == ""
        subprocess.run(
            [bash, "-c", shell_command],
            env=env,
            check=True,
            capture_output=True,
        )

    expected_root = _path_for_bash(bash, vault)
    records = [line.split("\t", 2) for line in uv_log.read_text(encoding="utf-8").splitlines()]
    assert {record[2] for record in records} == {
        "run python scripts/scheduled_nightly.py",
        "run python scripts/scheduled_weekly.py",
    }
    assert all(record[:2] == [expected_root, expected_root] for record in records)


@pytest.mark.parametrize("xdg_kind", ["unset", "empty", "relative", "alias", "distinct"])
def test_powershell_xdg_resolver_validates_and_deduplicates_paths(tmp_path, xdg_kind):
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is unavailable")
    source = (ROOT / "install.ps1").read_text(encoding="utf-8")
    function = _extract_braced_function(source, "function Get-OpenCodeConfigs")
    home = tmp_path / "home's folder"
    compatibility = (home / ".config" / "opencode").resolve()
    values = {
        "empty": "",
        "relative": ("relative/config", "C:relative", "\\relative"),
        "alias": str(home / ".config" / ".." / ".config"),
        "distinct": str(tmp_path / "XDG's config dir"),
    }
    selected = values.get(xdg_kind)
    candidates = selected if isinstance(selected, tuple) else (selected,)
    for candidate in candidates:
        env = {**os.environ, "USERPROFILE": str(home)}
        if xdg_kind == "unset":
            env.pop("XDG_CONFIG_HOME", None)
        else:
            env["XDG_CONFIG_HOME"] = candidate
        command = f"{function}\n@((Get-OpenCodeConfigs)) | ConvertTo-Json -Compress"

        result = subprocess.run(
            [pwsh, "-NoProfile", "-Command", command],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        parsed = json.loads(result.stdout)
        paths = [parsed] if isinstance(parsed, str) else parsed

        if xdg_kind == "distinct":
            assert paths == [
                str((Path(candidate) / "opencode").resolve()),
                str(compatibility),
            ]
        else:
            assert paths == [str(compatibility)]


def test_windows_installer_adds_distinct_compatibility_path_without_duplicates():
    """Windows keeps ~/.config compatibility without copying twice."""
    install_ps1 = (ROOT / "install.ps1").read_text(encoding="utf-8")

    assert "$windowsOpenCodeConfig" in install_ps1
    assert "[System.IO.Path]::GetFullPath" in install_ps1
    assert "[System.StringComparer]::OrdinalIgnoreCase" in install_ps1
    assert "$openCodeConfigSet.Add" in install_ps1


@pytest.mark.parametrize("newline", (b"\n", b"\r\n"), ids=("lf", "crlf"))
def test_powershell_profile_wrapper_preserves_existing_line_endings_and_content(
    tmp_path,
    newline,
):
    profile = tmp_path / "Microsoft.PowerShell_profile.ps1"
    original = newline.join((b"# unrelated profile", b"Set-Alias keep Get-Item", b""))
    profile.write_bytes(original)

    result = _run_powershell_profile_update(profile)

    assert result.stdout.strip() == "changed"
    assert profile.read_bytes() == original + POWERSHELL_CODEX_WRAPPER.encode() + newline


def test_powershell_profile_wrapper_adds_real_boundary_after_unterminated_line(tmp_path):
    profile = tmp_path / "Microsoft.PowerShell_profile.ps1"
    original = b"Set-Alias keep Get-Item"
    profile.write_bytes(original)

    _run_powershell_profile_update(profile)

    newline = os.linesep.encode()
    assert profile.read_bytes() == (
        original + newline + POWERSHELL_CODEX_WRAPPER.encode() + newline
    )


def test_powershell_profile_wrapper_handles_empty_profile_idempotently(tmp_path):
    profile = tmp_path / "Microsoft.PowerShell_profile.ps1"
    profile.write_bytes(b"")

    first = _run_powershell_profile_update(profile)
    first_bytes = profile.read_bytes()
    second = _run_powershell_profile_update(profile)

    assert first.stdout.strip() == "changed"
    assert second.stdout.strip() == "unchanged"
    assert profile.read_bytes() == first_bytes
    assert first_bytes == POWERSHELL_CODEX_WRAPPER.encode() + os.linesep.encode()
    assert first_bytes.count(POWERSHELL_CODEX_WRAPPER.encode()) == 1


@pytest.mark.parametrize(
    ("bom", "encoding"),
    (
        pytest.param(b"\xef\xbb\xbf", "utf-8", id="utf8-bom"),
        pytest.param(b"\xff\xfe", "utf-16-le", id="utf16-le-bom"),
    ),
)
def test_powershell_profile_wrapper_preserves_bom_and_encoding(tmp_path, bom, encoding):
    profile = tmp_path / "Microsoft.PowerShell_profile.ps1"
    original_text = "# preserve caf\u00e9\r\n"
    original = bom + original_text.encode(encoding)
    profile.write_bytes(original)

    _run_powershell_profile_update(profile)

    expected = original + (POWERSHELL_CODEX_WRAPPER + "\r\n").encode(encoding)
    assert profile.read_bytes() == expected


def test_powershell_profile_wrapper_ignores_substring_decoys(tmp_path):
    profile = tmp_path / "Microsoft.PowerShell_profile.ps1"
    original = (
        b"# codex-memory-wrapper is documented here, not loaded\n"
        b"$codexMemoryWrapperBackup = 'leave this unrelated value alone'\n"
    )
    profile.write_bytes(original)

    _run_powershell_profile_update(profile)

    updated = profile.read_bytes()
    assert updated.startswith(original)
    assert updated.splitlines().count(POWERSHELL_CODEX_WRAPPER.encode()) == 1


def test_powershell_profile_publish_failure_leaves_original_and_cleans_temp(tmp_path):
    profile = tmp_path / "Microsoft.PowerShell_profile.ps1"
    original = b"# original must survive\r\n"
    profile.write_bytes(original)
    publish_call = (
        "[System.IO.File]::Replace(\n"
        "                $temporaryPath,\n"
        "                $targetPath,\n"
        "                [System.Management.Automation.Language.NullString]::Value,\n"
        "                $false\n"
        "            )"
    )

    def inject_failure(function):
        assert publish_call in function
        return function.replace(publish_call, 'throw "injected before replace"', 1)

    result = _run_powershell_profile_update(
        profile,
        source_transform=inject_failure,
        check=False,
    )

    assert result.returncode != 0
    assert profile.read_bytes() == original
    assert not list(tmp_path.glob(".*.llm-wiki.*.tmp"))


def test_powershell_profile_masks_readonly_before_failed_publication(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows read-only file attributes are unavailable")

    profile = tmp_path / "Microsoft.PowerShell_profile.ps1"
    original = b"# read-only original must survive\r\n"
    profile.write_bytes(original)
    profile.chmod(stat.S_IREAD)
    original_attributes = profile.stat().st_file_attributes
    assert original_attributes & stat.FILE_ATTRIBUTE_READONLY
    attribute_call = (
        "[System.IO.File]::SetAttributes($temporaryPath, $temporaryAttributes)"
    )

    def inject_failure(function):
        assert attribute_call in function
        probe_and_fail = (
            attribute_call
            + "\n            $observedTemporaryAttributes = "
            "[System.IO.File]::GetAttributes($temporaryPath)\n"
            "            if (($observedTemporaryAttributes -band "
            "[System.IO.FileAttributes]::ReadOnly) -ne 0) {\n"
            '                throw "temporary file retained ReadOnly"\n'
            "            }\n"
            '            throw "injected after attribute copy"'
        )
        return function.replace(attribute_call, probe_and_fail, 1)

    try:
        result = _run_powershell_profile_update(
            profile,
            source_transform=inject_failure,
            check=False,
        )

        assert result.returncode != 0
        assert "injected after attribute copy" in result.stderr
        assert "temporary file retained ReadOnly" not in result.stderr
        assert profile.read_bytes() == original
        assert profile.stat().st_file_attributes == original_attributes
        assert not list(tmp_path.glob(".*.llm-wiki.*.tmp"))
    finally:
        profile.chmod(stat.S_IWRITE)
        for temporary in tmp_path.glob(".*.llm-wiki.*.tmp"):
            temporary.chmod(stat.S_IWRITE)
            temporary.unlink()


def test_powershell_profile_symlink_updates_referent_without_replacing_link(tmp_path):
    referent_dir = tmp_path / "profile storage"
    link_dir = tmp_path / "home"
    referent_dir.mkdir()
    link_dir.mkdir()
    referent = referent_dir / "actual-profile.ps1"
    referent.write_bytes(b"# referent\n")
    profile = link_dir / "Microsoft.PowerShell_profile.ps1"
    try:
        profile.symlink_to(referent)
    except OSError as exc:
        pytest.skip(f"profile symlink creation is unavailable: {exc}")

    _run_powershell_profile_update(profile)

    assert profile.is_symlink()
    assert profile.resolve() == referent.resolve()
    assert referent.read_bytes() == (
        b"# referent\n" + POWERSHELL_CODEX_WRAPPER.encode() + b"\n"
    )
    assert not list(referent_dir.glob(".*.llm-wiki.*.tmp"))
    assert not list(link_dir.glob(".*.llm-wiki.*.tmp"))


def test_windows_installer_uses_atomic_profile_helper_not_add_content():
    source = (ROOT / "install.ps1").read_text(encoding="utf-8")

    assert "function Update-PowerShellProfile" in source
    assert "Update-PowerShellProfile -ProfilePath $profilePath" in source
    assert "Add-Content" not in source


@pytest.mark.parametrize("installer", ["install.ps1", "install.sh"])
@pytest.mark.parametrize("command", ["uv sync --locked --quiet", "uv run pytest -q"])
def test_installers_fail_closed_before_configuration(installer, command):
    """Dependency or test failure must exit nonzero before configuration."""
    source = (ROOT / installer).read_text(encoding="utf-8")
    command_at = source.index(command)
    configuration_at = source.index("Setting environment variables")
    failure_guard = source[max(0, command_at - 10):configuration_at]

    assert command_at < configuration_at
    if installer.endswith(".ps1"):
        assert "$LASTEXITCODE -ne 0" in failure_guard
        assert "Fail " in failure_guard
    else:
        assert re.search(rf"if\s+!\s+{re.escape(command)}(?:\s|;)", failure_guard)
        assert "fail " in failure_guard


# ─── 4. CHANGELOG latest version matches pyproject.toml ─────────────

def test_changelog_latest_version_matches_pyproject():
    """The first [X.Y.Z] header in CHANGELOG must equal pyproject's version."""
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    m_cl = re.search(r"^##\s*\[(\d+(?:\.\d+)*)\]", changelog, re.MULTILINE)
    assert m_cl, "could not find a version header in CHANGELOG.md"
    cl_ver = m_cl.group(1)

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m_pp = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
    assert m_pp, "could not parse version from pyproject.toml"
    pp_ver = m_pp.group(1)

    assert cl_ver == pp_ver, (
        f"CHANGELOG latest version [{cl_ver}] != pyproject version [{pp_ver}]"
    )


# ─── 5. CHANGELOG test count matches live suite ─────────────────────

def test_changelog_unreleased_test_count_matches_live():
    """Unreleased reports the live count without rewriting release history."""
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = re.search(
        r"^## \[Unreleased\]\s*$\n(?P<section>.*?)(?=^## \[\d)",
        changelog,
        re.MULTILINE | re.DOTALL,
    )
    assert unreleased, "CHANGELOG.md has no Unreleased section before release history"
    section = unreleased.group("section")

    count_match = re.search(r"(\d+)\s+tests?\b", section)
    assert count_match, "no 'N tests' claim in CHANGELOG Unreleased section"
    claimed = int(count_match.group(1))

    live = _collect_test_count()
    assert claimed == live, (
        f"CHANGELOG Unreleased claims {claimed} tests but live suite collects {live}; "
        f"update the operational count without changing historical releases"
    )


# ─── 6. ARCHITECTURE.md must not cite Recall@2 ──────────────────────

def test_architecture_no_recall_at_2():
    """Recall@2 is not in benchmark/report.md; docs must not cite it."""
    arch = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    assert "Recall@2" not in arch, (
        "docs/ARCHITECTURE.md cites Recall@2, which is absent from "
        "benchmark/report.md — remove or replace with a reported metric"
    )


@pytest.mark.parametrize(
    "doc_name",
    ("docs/ARCHITECTURE.md", "docs/USER-GUIDE.md"),
)
def test_capture_docs_describe_mutation_only_direct_file_tools(doc_name):
    text = (ROOT / doc_name).read_text(encoding="utf-8")
    lowered = " ".join(text.casefold().split())

    assert "mutation-only direct file tools" in lowered
    assert "shell, read, and search activity is not captured" in lowered
    assert "edit/write/bash" not in lowered


# ─── 7. Skills' allowed-tools reference existing scripts ────────────

def test_skills_allowed_tools_reference_existing_scripts():
    """Direct Bash(script ...) references in skills must point to real files."""
    skills_dir = ROOT / "skills"
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        text = skill_md.read_text(encoding="utf-8")
        for bash_call in re.findall(r"Bash\(([^)]*)\)", text):
            # Ignore runtime commands ("uv run ...").
            if bash_call.strip().startswith("uv run"):
                continue
            for script_rel in re.findall(r"(scripts/\S+\.py)", bash_call):
                assert (ROOT / script_rel).is_file(), (
                    f"{skill_md.relative_to(ROOT)}: allowed-tools references "
                    f"{script_rel} which does not exist"
                )


# ─── 8. README must not invent agentmemory Recall@10 ────────────────

def test_readme_recall_at_10_agentmemory():
    """README must not claim a competitor Recall@10 % unless report.md has it."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    report = (ROOT / "benchmark" / "report.md").read_text(encoding="utf-8")

    report_has_recall10 = "Recall@10" in report

    row = re.search(
        r"\|\s*Recall@10\s*\|([^|]+)\|([^|]+)\|([^|]+)\|([^|]+)\|", readme
    )
    if not row:
        return  # no Recall@10 row — nothing to guard

    cells = [c.strip() for c in row.groups()]
    # cells[0] = LLM Wiki (allowed to have a %); rest are competitors.
    if not report_has_recall10:
        for cell in cells[1:]:
            assert not re.search(r"\d+\.?\d*%", cell), (
                f"README Recall@10 competitor cell '{cell}' has a percentage "
                f"not backed by benchmark/report.md — use 'n/a'"
            )


# ─── 9. Lint check count in docs must match code ────────────────────

def test_lint_check_count_matches_code():
    """The lint check count in README/docs must match lint_memory.py source."""
    lint_src = (ROOT / "scripts" / "lint_memory.py").read_text(encoding="utf-8")
    # Count registered check categories in run_checks()
    checks = re.findall(r'checks\.append\(', lint_src)
    if not checks:
        # Alternative: count check_ function definitions
        checks = re.findall(r'^def check_', lint_src, re.MULTILINE)
    actual = len(checks)
    assert actual > 0, "Could not count lint checks in lint_memory.py"

    for doc_name in ("README.md", "README.ru.md", "README.zh-CN.md",
                      "docs/ARCHITECTURE.md"):
        doc = (ROOT / doc_name).read_text(encoding="utf-8")
        # Find "N lint checks" or "N checks" patterns
        for m in re.finditer(r"(\d+)\s*(?:lint[- ]?checks?|structural\s+(?:lint\s+)?checks?)", doc, re.IGNORECASE):
            claimed = int(m.group(1))
            # The doc may say "13 structural" (correct if total is 14 with contradiction)
            # or "14" total. Accept either if it matches actual or actual-1.
            assert claimed in (actual, actual - 1), (
                f"{doc_name}: claims {claimed} lint checks but code has {actual}. "
                f"Update docs to match."
            )


# ─── 10. No standalone root cognee/ in docs ─────────────────────────

def test_no_standalone_cognee_in_docs():
    """Docs must use cache/cognee/ not standalone root cognee/."""
    structure = (ROOT / "docs" / "STRUCTURE.md").read_text(encoding="utf-8")
    # Extract canonical runtime dirs from STRUCTURE.md
    assert "cache/cognee" in structure, "STRUCTURE.md must document cache/cognee/"

    for doc_name in ("README.md", "README.ru.md", "README.zh-CN.md",
                      "docs/USER-GUIDE.md", "CONTRIBUTING.md"):
        doc = (ROOT / doc_name).read_text(encoding="utf-8")
        # Find standalone cognee/ not preceded by cache/
        for m in re.finditer(r"(?<!cache/)(?<!cache\\)\bcognee/", doc):
            line = doc[:m.start()].count("\n") + 1
            pytest.fail(
                f"{doc_name}:{line}: standalone 'cognee/' found — "
                f"should be 'cache/cognee/' per STRUCTURE.md"
            )


# ─── 11. Installer version comments match pyproject.toml ────────────

def test_installer_version_matches_pyproject():
    """Installer version-tag comments must match pyproject.toml version."""
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    version_match = re.search(r'version\s*=\s*"(\d+\.\d+\.\d+)"', pyproject)
    assert version_match, "No version in pyproject.toml"
    current_version = version_match.group(1)

    for installer in ("install.sh", "install.ps1"):
        src = (ROOT / installer).read_text(encoding="utf-8")
        # Find version-tag references like v3.3.3
        for m in re.finditer(r"v(\d+\.\d+\.\d+)", src):
            tag_version = m.group(1)
            if tag_version != current_version:
                line = src[:m.start()].count("\n") + 1
                pytest.fail(
                    f"{installer}:{line}: references v{tag_version} but "
                    f"pyproject.toml is {current_version}. Update installer comment."
                )


# ─── 12. All daily-log writers use shared lock ──────────────────────

def test_all_daily_writers_use_lock():
    """Scripts that write to daily logs must use _daily_lock or append_daily."""
    daily_writers = []
    for py in (ROOT / "scripts").glob("*.py"):
        src = py.read_text(encoding="utf-8")
        # Check if the script writes to a daily log file
        if re.search(
            r"(daily.*\.open\s*\(\s*['\"][^'\"]*[awx+]|append.*daily|DAILY_DIR.*\.write)",
            src,
        ):
            if py.name in ("daily_log_append.py", "memory_state.py"):
                continue  # These define the lock/append infrastructure
            daily_writers.append(py)

    for py in daily_writers:
        src = py.read_text(encoding="utf-8")
        has_lock = "_daily_lock" in src or "append_daily" in src or "locked_append" in src
        if not has_lock:
            pytest.fail(
                f"{py.name}: writes to daily log without using _daily_lock() "
                f"or append_daily(). All daily-log writes must be lock-protected."
            )


# ─── 13. Clean-clone: all imports in tracked scripts resolve to tracked files ─

def test_all_script_imports_resolve_in_git():
    """Every local import in scripts/*.py must resolve to a file tracked by Git.

    This catches the #1 recurring issue across audit rounds: new .py files
    created during fixes but never `git add`ed. On a clean clone, these
    cause ModuleNotFoundError before any test can run.
    """
    import subprocess

    # Get list of tracked files
    r = subprocess.run(
        ["git", "ls-files", "scripts/", "tests/"],
        cwd=ROOT, capture_output=True, text=True,
    )
    tracked = set()
    for line in r.stdout.strip().splitlines():
        tracked.add(line.split("/")[-1])  # filename only
        tracked.add(line)  # full path

    # Scan all tracked scripts for local imports
    for py in sorted((ROOT / "scripts").glob("*.py")):
        rel = f"scripts/{py.name}"
        if rel not in tracked and py.name not in tracked:
            continue  # untracked script — skip (will be caught by git status)
        src = py.read_text(encoding="utf-8")
        # Find local imports (not stdlib, not pip packages)
        for m in re.finditer(r"^\s*(?:from|import)\s+(\w+)", src, re.MULTILINE):
            mod_name = m.group(1)
            # Skip stdlib and known external packages
            if mod_name in ("os", "sys", "re", "json", "time", "datetime", "pathlib",
                            "hashlib", "subprocess", "argparse", "contextlib",
                            "io", "math", "secrets", "threading", "typing",
                            "collections", "functools", "itertools", "enum",
                            "dataclasses", "abc", "copy", "tempfile", "shutil",
                            "importlib", "traceback", "textwrap", "string",
                            "unittest", "pytest", "__future__",
                            "datetime", "warnings"):
                continue
            # Check if this is a local module (a .py file in scripts/)
            potential_file = ROOT / "scripts" / f"{mod_name}.py"
            if potential_file.exists():
                # It's a local import — must be tracked
                if f"scripts/{mod_name}.py" not in tracked and mod_name + ".py" not in tracked:
                    pytest.fail(
                        f"scripts/{py.name}: imports '{mod_name}' which exists as "
                        f"scripts/{mod_name}.py but is NOT tracked by Git. "
                        f"Run: git add scripts/{mod_name}.py"
                    )


# ─── 14. No untracked .py files that are imported by tracked code ──────────

def test_no_untracked_imported_modules():
    """No untracked .py file in scripts/ should be importable by tracked code.

    This is the clean-clone test: if a new helper module is created during
    a fix but not committed, the next clean clone breaks. This test catches
    that before it ships.
    """
    import subprocess

    # Get untracked .py files
    r = subprocess.run(
        ["git", "status", "--short", "--porcelain", "scripts/"],
        cwd=ROOT, capture_output=True, text=True,
    )
    untracked = []
    for line in r.stdout.strip().splitlines():
        if line.startswith("??") and line.endswith(".py"):
            name = line.split("/")[-1].strip()
            untracked.append(name.replace(".py", ""))

    if not untracked:
        return  # No untracked .py files — clean

    # Check if any tracked script imports these untracked modules
    for py in sorted((ROOT / "scripts").glob("*.py")):
        src = py.read_text(encoding="utf-8")
        for mod in untracked:
            if re.search(rf"(?:from|import)\s+{mod}\b", src):
                pytest.fail(
                    f"scripts/{py.name}: imports '{mod}' which is UNTRACKED. "
                    f"Run: git add scripts/{mod}.py — clean clone will break."
                )
