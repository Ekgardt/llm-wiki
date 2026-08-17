from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

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


def _bare_repository(tmp_path: Path, *, missing: str | None = None) -> tuple[Path, str]:
    source = tmp_path / "source"
    remote = tmp_path / "remote.git"
    source.mkdir()
    _git(source, "init")
    for relative in REQUIRED_FILES:
        if relative == missing:
            continue
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative == "install.sh":
            path.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n%s\\n%s' \"${BASH_SOURCE[0]}\" \"$LLM_WIKI_ROOT\" \"$PWD\" > \"$CALLER_MARKER\"\n",
                encoding="utf-8",
            )
            path.chmod(0o755)
        elif relative == "install.ps1":
            path.write_text(
                "[System.IO.File]::WriteAllText($env:CALLER_MARKER, "
                "$PSCommandPath + [Environment]::NewLine + $env:LLM_WIKI_ROOT + "
                "[Environment]::NewLine + (Get-Location).Path)\n",
                encoding="utf-8",
            )
        else:
            path.write_text(f"fixture {relative}\n", encoding="utf-8")
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


def _shell_function(source: str, name: str) -> str:
    match = re.search(rf"^{re.escape(name)}\(\) \{{", source, re.MULTILINE)
    assert match is not None, f"missing shell function {name}"
    depth = 0
    quoted: str | None = None
    escaped = False
    for index in range(match.start(), len(source)):
        char = source[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if quoted:
            if char == quoted:
                quoted = None
            continue
        if char in "'\"":
            quoted = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[match.start() : index + 1]
    raise AssertionError(f"unterminated shell function {name}")


def _powershell_functions(source: Path, names: tuple[str, ...]) -> str:
    return textwrap.dedent(
        f"""
        $tokens = $null
        $errors = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile(
            {json.dumps(str(source))}, [ref]$tokens, [ref]$errors)
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
    if shell == "bash":
        executable = _bash()
        if executable is None:
            pytest.skip("supported POSIX Bash unavailable")
        result = subprocess.run(
            [executable, "-s"],
            input=(ROOT / "install.sh").read_text(encoding="utf-8"),
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    else:
        executable = _pwsh()
        if executable is None:
            pytest.skip("PowerShell unavailable")
        result = subprocess.run(
            [executable, "-NoProfile", "-NonInteractive", "-Command", "-"],
            input=(ROOT / "install.ps1").read_text(encoding="utf-8"),
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    assert result.returncode != 0
    assert "full 40-hex commit OID" in result.stdout + result.stderr
    assert not (tmp_path / "home" / "LLM-wiki").exists()


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
    environment = os.environ.copy()
    environment.update(
        HOME=str(home),
        USERPROFILE=str(home),
        LLM_WIKI_COMMIT=oid.upper(),
        CALLER_MARKER=str(marker),
    )
    if shell == "bash":
        executable = _bash()
        if executable is None:
            pytest.skip("supported POSIX Bash unavailable")
        source = (ROOT / "install.sh").read_text(encoding="utf-8").replace(
            f'REPOSITORY_URL="{REPOSITORY_URL}"',
            f"REPOSITORY_URL={shlex_quote(str(remote))}",
        )
        result = subprocess.run(
            [executable, "-s"],
            input=source,
            cwd=caller,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    else:
        executable = _pwsh()
        if executable is None:
            pytest.skip("PowerShell unavailable")
        source = (ROOT / "install.ps1").read_text(encoding="utf-8").replace(
            f'$repositoryUrl = "{REPOSITORY_URL}"',
            f"$repositoryUrl = {powershell_quote(str(remote))}",
        )
        result = subprocess.run(
            [executable, "-NoProfile", "-NonInteractive", "-Command", "-"],
            input=source,
            cwd=caller,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    assert result.returncode == 0, result.stdout + result.stderr
    values = marker.read_text(encoding="utf-8").splitlines()
    checkout = home / "LLM-wiki"
    assert Path(values[0]).resolve() in {
        (checkout / "install.sh").resolve(),
        (checkout / "install.ps1").resolve(),
    }
    assert Path(values[1]).resolve() == checkout.resolve()
    assert Path(values[2]).resolve() == caller.resolve()
    assert _git(checkout, "rev-parse", "HEAD") == oid
    assert _git(checkout, "remote", "get-url", "origin") == str(remote)


def shlex_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


def powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


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
    if shell == "bash":
        executable = _bash()
        if executable is None:
            pytest.skip("supported POSIX Bash unavailable")
        source = (ROOT / "install.sh").read_text(encoding="utf-8").replace(
            f'REPOSITORY_URL="{REPOSITORY_URL}"',
            f"REPOSITORY_URL={shlex_quote(str(remote))}",
        )
        result = subprocess.run(
            [executable, "-s"], input=source, cwd=tmp_path, env=environment,
            capture_output=True, text=True, timeout=30, check=False,
        )
    else:
        executable = _pwsh()
        if executable is None:
            pytest.skip("PowerShell unavailable")
        source = (ROOT / "install.ps1").read_text(encoding="utf-8").replace(
            f'$repositoryUrl = "{REPOSITORY_URL}"',
            f"$repositoryUrl = {powershell_quote(str(remote))}",
        )
        result = subprocess.run(
            [executable, "-NoProfile", "-NonInteractive", "-Command", "-"],
            input=source, cwd=tmp_path, env=environment, capture_output=True,
            text=True, timeout=30, check=False,
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


def _invoke_push_helper(
    repository: Path, shell: str, *, created: bool, explicit: bool
) -> None:
    if shell == "bash":
        executable = _bash()
        if executable is None:
            pytest.skip("supported POSIX Bash unavailable")
        source = (ROOT / "install.sh").read_text(encoding="utf-8")
        functions = "\n".join(
            _shell_function(source, name)
            for name in ("protect_push_urls", "protect_push_urls_if_authorized")
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
        result = _run([executable, str(runner)], cwd=repository)
    else:
        executable = _pwsh()
        if executable is None:
            pytest.skip("PowerShell unavailable")
        command = _powershell_functions(
            ROOT / "install.ps1",
            ("Invoke-NativeCommand", "Protect-PushUrls", "Protect-PushUrlsIfAuthorized"),
        ) + textwrap.dedent(
            f"""
            Protect-PushUrlsIfAuthorized `
                -VaultRoot {json.dumps(str(repository))} `
                -InstallerCreatedClone ${str(created).lower()} `
                -ProtectPush ${str(explicit).lower()}
            """
        )
        result = _run(
            [executable, "-NoProfile", "-NonInteractive", "-Command", command],
            cwd=repository,
        )
    assert result.returncode == 0, result.stdout + result.stderr
