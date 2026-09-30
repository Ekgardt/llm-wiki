from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.powershell_literal import ps_literal

ROOT = Path(__file__).resolve().parent.parent
REPOSITORY_URL = "https://github.com/Ekgardt/llm-wiki.git"
REQUIRED_FILES = (
    "pyproject.toml",
    "uv.lock",
    "install.sh",
    "install.ps1",
    "scripts/installer_config.py",
    "scripts/install_control.py",
)


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None):
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )


def _git(cwd: Path, *arguments: str) -> str:
    result = _run(["git", *arguments], cwd=cwd)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _write_required_file(path: Path, relative: str) -> None:
    """One required file: the two installers record how they were called."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if relative == "install.sh":
        path.write_text(
            "#!/usr/bin/env bash\n"
            "printf '%s\\n%s\\n%s' \"${BASH_SOURCE[0]}\" \"$LLM_WIKI_ROOT\" \"$PWD\" > \"$CALLER_MARKER\"\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        return
    if relative == "install.ps1":
        path.write_text(
            "[System.IO.File]::WriteAllText($env:CALLER_MARKER, "
            "$PSCommandPath + [Environment]::NewLine + $env:LLM_WIKI_ROOT + "
            "[Environment]::NewLine + (Get-Location).Path)\n",
            encoding="utf-8",
        )
        return
    path.write_text(f"fixture {relative}\n", encoding="utf-8")


def _bare_repository(tmp_path: Path, *, missing: str | None = None) -> tuple[Path, str]:
    source = tmp_path / "source"
    remote = tmp_path / "remote.git"
    source.mkdir()
    _git(source, "init")
    for relative in REQUIRED_FILES:
        if relative == missing:
            continue
        _write_required_file(source / relative, relative)
    _git(source, "add", ".")
    _git(
        source,
        "-c",
        "user.name=Installer Test",
        "-c",
        "user.email=installer@example.test",
        "commit",
        "-m",
        "fixture",
    )
    oid = _git(source, "rev-parse", "HEAD")
    _git(tmp_path, "init", "--bare", str(remote))
    _git(source, "remote", "add", "origin", str(remote))
    _git(source, "push", "origin", "HEAD:refs/heads/main")
    return remote, oid


def _bash() -> str | None:
    if os.name == "nt":
        return None
    return shutil.which("bash")


def _pwsh() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


@dataclass
class _ShellScan:
    """Where a shell-function scan is: brace depth, open quote, pending escape."""

    depth: int = 0
    quoted: str | None = None
    escaped: bool = False


_BRACE_DEPTH = {"{": 1, "}": -1}


def _scan_unquoted(state: _ShellScan, char: str) -> bool:
    """Advance outside quotes; True when the function's closing brace was read."""
    if char in "'\"":
        state.quoted = char
        return False
    state.depth += _BRACE_DEPTH.get(char, 0)
    return char == "}" and state.depth == 0


def _scan_char(state: _ShellScan, char: str) -> bool:
    """Advance one character; True when the function ends here."""
    if state.escaped or char == "\\":
        state.escaped = char == "\\"
        return False
    if state.quoted:
        state.quoted = None if char == state.quoted else state.quoted
        return False
    return _scan_unquoted(state, char)


def _shell_function(source: str, name: str) -> str:
    match = re.search(rf"^{re.escape(name)}\(\) \{{", source, re.MULTILINE)
    assert match is not None, f"missing shell function {name}"
    state = _ShellScan()
    for index in range(match.start(), len(source)):
        if _scan_char(state, source[index]):
            return source[match.start() : index + 1]
    raise AssertionError(f"unterminated shell function {name}")


def _powershell_functions(source: Path, names: tuple[str, ...]) -> str:
    return textwrap.dedent(
        f"""
        $tokens = $null
        $errors = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile(
            {ps_literal(str(source))}, [ref]$tokens, [ref]$errors)
        if ($errors.Count) {{ throw ($errors | Out-String) }}
        foreach ($name in @({', '.join(json.dumps(name) for name in names)})) {{
            $fn = $ast.Find({{ param($node)
                $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
                $node.Name -eq $name
            }}, $true)
            if ($null -eq $fn) {{ throw "missing PowerShell function $name" }}
            Invoke-Expression $fn.Extent.Text
        }}
        """
    )


def _bash_stdin_command() -> list[str]:
    executable = _bash()
    if executable is None:
        pytest.skip("supported POSIX Bash unavailable")
    return [executable, "-s"]


def _pwsh_stdin_command() -> list[str]:
    executable = _pwsh()
    if executable is None:
        pytest.skip("PowerShell unavailable")
    return [executable, "-NoProfile", "-NonInteractive", "-Command", "-"]


def _bash_source_from(remote: Path) -> str:
    return (ROOT / "install.sh").read_text(encoding="utf-8").replace(
        f'REPOSITORY_URL="{REPOSITORY_URL}"',
        f"REPOSITORY_URL={shlex_quote(str(remote))}",
    )


def _pwsh_source_from(remote: Path) -> str:
    return (ROOT / "install.ps1").read_text(encoding="utf-8").replace(
        f'$repositoryUrl = "{REPOSITORY_URL}"',
        f"$repositoryUrl = {ps_literal(str(remote))}",
    )


# Per shell: the command that reads an installer on stdin (or skips when the shell
# is absent), the installer itself, and the installer pointed at another remote.
_STDIN_COMMANDS = {"bash": _bash_stdin_command, "powershell": _pwsh_stdin_command}
_INSTALLERS = {"bash": ROOT / "install.sh", "powershell": ROOT / "install.ps1"}
_REMOTE_SOURCES = {"bash": _bash_source_from, "powershell": _pwsh_source_from}


@pytest.mark.parametrize(
    "value",
    [None, "", "main", "v4.0.0", "abc123", "g" * 40, "a" * 39, "a" * 41],
)
@pytest.mark.parametrize("shell", ["bash", "powershell"])
def test_remote_bootstrap_rejects_non_full_oid(
    tmp_path: Path, value: str | None, shell: str
) -> None:
    environment = os.environ.copy()
    environment.update(HOME=str(tmp_path / "home"), USERPROFILE=str(tmp_path / "home"))
    if value is None:
        environment.pop("LLM_WIKI_COMMIT", None)
    else:
        environment["LLM_WIKI_COMMIT"] = value
    command = _STDIN_COMMANDS[shell]()
    result = subprocess.run(
        command,
        input=_INSTALLERS[shell].read_text(encoding="utf-8"),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode != 0
    assert "full 40-hex commit OID" in result.stdout + result.stderr
    assert not (tmp_path / "home" / "LLM-wiki").exists()


def _pipe_environment(home: Path, oid: str, marker: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        HOME=str(home),
        USERPROFILE=str(home),
        LLM_WIKI_COMMIT=oid.upper(),
        CALLER_MARKER=str(marker),
    )
    return environment


def _assert_the_checkout_ran(values: list[str], checkout: Path, caller: Path) -> None:
    """The marker names the installer that ran, its checkout, and the caller's directory."""
    assert Path(values[0]).resolve() in {
        (checkout / "install.sh").resolve(),
        (checkout / "install.ps1").resolve(),
    }
    assert Path(values[1]).resolve() == checkout.resolve()
    assert Path(values[2]).resolve() == caller.resolve()


def _assert_the_exact_head(checkout: Path, oid: str, remote: Path) -> None:
    assert _git(checkout, "rev-parse", "HEAD") == oid
    assert _git(checkout, "remote", "get-url", "origin") == str(remote)


@pytest.mark.parametrize("shell", ["bash", "powershell"])
def test_pipe_mode_ignores_caller_checkout_and_verifies_exact_head(
    tmp_path: Path, shell: str
) -> None:
    remote, oid = _bare_repository(tmp_path)
    caller = tmp_path / "caller"
    caller.mkdir()
    (caller / "pyproject.toml").write_text("caller trap\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    marker = tmp_path / f"{shell}.marker"
    environment = _pipe_environment(home, oid, marker)
    command = _STDIN_COMMANDS[shell]()
    result = subprocess.run(
        command,
        input=_REMOTE_SOURCES[shell](remote),
        cwd=caller,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    checkout = home / "LLM-wiki"
    _assert_the_checkout_ran(marker.read_text(encoding="utf-8").splitlines(), checkout, caller)
    _assert_the_exact_head(checkout, oid, remote)


def shlex_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)



@pytest.mark.parametrize("shell", ["bash", "powershell"])
def test_remote_bootstrap_rejects_missing_required_file(
    tmp_path: Path, shell: str
) -> None:
    remote, oid = _bare_repository(tmp_path, missing="scripts/installer_config.py")
    home = tmp_path / "home"
    home.mkdir()
    environment = os.environ.copy()
    environment.update(
        HOME=str(home), USERPROFILE=str(home), LLM_WIKI_COMMIT=oid
    )
    command = _STDIN_COMMANDS[shell]()
    result = subprocess.run(
        command, input=_REMOTE_SOURCES[shell](remote), cwd=tmp_path, env=environment,
        capture_output=True, text=True, timeout=30, check=False,
    )

    assert result.returncode != 0
    assert "missing scripts/installer_config.py" in result.stdout + result.stderr


def _repository_with_remotes(tmp_path: Path) -> tuple[Path, dict[str, list[str]]]:
    repository = tmp_path / "checkout"
    repository.mkdir()
    _git(repository, "init")
    expected = {
        "origin": ["https://example.test/fetch-one", "https://example.test/fetch-two"],
        "backup": ["ssh://example.test/backup"],
    }
    for remote, urls in expected.items():
        _git(repository, "remote", "add", remote, urls[0])
        for url in urls[1:]:
            _git(repository, "remote", "set-url", "--add", remote, url)
        _git(repository, "remote", "set-url", "--add", "--push", remote, "old-one")
        _git(repository, "remote", "set-url", "--add", "--push", remote, "old-two")
    return repository, expected


@pytest.mark.parametrize("shell", ["bash", "powershell"])
def test_existing_checkout_keeps_remote_urls_without_explicit_option(
    tmp_path: Path, shell: str
) -> None:
    repository, expected_fetch = _repository_with_remotes(tmp_path)
    before = (repository / ".git" / "config").read_bytes()
    _invoke_push_helper(repository, shell, created=False, explicit=False)
    assert (repository / ".git" / "config").read_bytes() == before
    for remote, urls in expected_fetch.items():
        assert _git(repository, "remote", "get-url", "--all", remote).splitlines() == urls


@pytest.mark.parametrize("shell", ["bash", "powershell"])
@pytest.mark.parametrize("created", [False, True])
def test_authorized_checkout_disables_every_remote_push_url(
    tmp_path: Path, shell: str, created: bool
) -> None:
    repository, expected_fetch = _repository_with_remotes(tmp_path)
    _invoke_push_helper(repository, shell, created=created, explicit=not created)
    for remote, urls in expected_fetch.items():
        assert _git(repository, "remote", "get-url", "--all", remote).splitlines() == urls
        assert _git(
            repository, "remote", "get-url", "--all", "--push", remote
        ).splitlines() == ["no-push"]


def _bash_push_helper(repository: Path, *, created: bool, explicit: bool):
    executable = _bash()
    if executable is None:
        pytest.skip("supported POSIX Bash unavailable")
    source = (ROOT / "install.sh").read_text(encoding="utf-8")
    functions = "\n".join(
        _shell_function(source, name)
        for name in ("clear_push_urls", "protect_remote_push_url", "protect_push_urls", "protect_push_urls_if_authorized")
    )
    runner = repository.parent / "push-helper.sh"
    runner.write_text(
        "set -euo pipefail\n"
        "fail() { printf '%s' \"$1\" >&2; return 1; }\n"
        + functions
        + f"\nVAULT_ROOT={shlex_quote(str(repository))}\n"
        + f"INSTALLER_CREATED_CLONE={1 if created else 0}\n"
        + f"PROTECT_PUSH={1 if explicit else 0}\n"
        + "protect_push_urls_if_authorized\n",
        encoding="utf-8",
    )
    return _run([executable, str(runner)], cwd=repository)


def _pwsh_push_helper(repository: Path, *, created: bool, explicit: bool):
    executable = _pwsh()
    if executable is None:
        pytest.skip("PowerShell unavailable")
    command = _powershell_functions(
        ROOT / "install.ps1",
        ("Invoke-NativeCommand", "Invoke-NativeProcess", "Write-NativeResult",
         "Protect-PushUrls", "Protect-PushUrlsIfAuthorized"),
    ) + textwrap.dedent(
        f"""
        Protect-PushUrlsIfAuthorized `
            -VaultRoot {ps_literal(str(repository))} `
            -InstallerCreatedClone ${str(created).lower()} `
            -ProtectPush ${str(explicit).lower()}
        """
    )
    return _run(
        [executable, "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=repository,
    )


_PUSH_HELPERS = {"bash": _bash_push_helper, "powershell": _pwsh_push_helper}


def _invoke_push_helper(
    repository: Path, shell: str, *, created: bool, explicit: bool
) -> None:
    result = _PUSH_HELPERS[shell](repository, created=created, explicit=explicit)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_windows_installer_claims_ownership_only_after_the_transaction_committed():
    """A failed step 6 must not print "owned" in step 7 (audit OPS-05)."""
    source = (ROOT / "install.ps1").read_text(encoding="utf-8")
    agents = source.split(
        "# --- 7. Detect and wire up agents ---------------------------------", 1
    )[1]
    owned = agents.index("Claude settings owned by the install transaction")
    guard = agents.index("$claudeAutomatic = -not $schedulerWarning")

    assert guard < owned
    assert "Claude settings not written: the install ownership transaction failed" in agents
