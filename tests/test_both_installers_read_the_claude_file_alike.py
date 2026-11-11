"""Both installers read `~/.claude.json` through one helper, and say what to do when it cannot.

PowerShell's ConvertFrom-Json refuses keys that differ only by case, which Claude
Code writes for two spellings of one project path, so install.ps1 called such a
file unreadable and printed no registration command; install.sh printed none for
an unreadable file either. See
docs/research/2026-09-27-both-installers-read-the-claude-file-alike.md.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from installer_config import claude_mcp_state

from tests.installer_bootstrap_helpers import _bash, _powershell_functions, _pwsh
from tests.powershell_literal import ps_literal
from tests.slow_machine import LONG_TIMEOUT
from tests.the_installer_says_what_it_needs_helpers import (
    INSTALL_PS1,
    INSTALL_SH,
    ROOT,
    STUB_ENV,
    UV_STUB_PS1,
    UV_STUB_SH,
    _shell_function,
    needs_bash,
    needs_pwsh,
)

VAULT = "/srv/vault"
CASE_KEYS = '{"projects": {"/srv/Repo": {}, "/srv/repo": {}}, "mcpServers": {"llm-wiki": {"args": ["--directory", "/srv/vault"]}}}'


def _config(tmp_path: Path, text: str | None) -> Path:
    path = tmp_path / "claude.json"
    if text is not None:
        path.write_text(text, encoding="utf-8")
    return path


def _servers(servers: object) -> str:
    return json.dumps({"mcpServers": servers})


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (None, "missing"),
        ("{not json", "unreadable"),
        ("[]", "absent"),
        (_servers({}), "absent"),
        (_servers({"llm-wiki": {"args": ["run", "--directory", VAULT]}}), "current"),
        (_servers({"llm-wiki": {"args": ["run", "--directory", "/srv/old-vault"]}}), "elsewhere"),
        (_servers({"llm-wiki": "not an entry"}), "elsewhere"),
        (CASE_KEYS, "current"),
    ],
)
def test_the_entry_state_is_read_from_the_file(tmp_path: Path, text: str | None, expected: str) -> None:
    assert claude_mcp_state(_config(tmp_path, text), VAULT) == expected


def test_a_vault_path_compares_as_the_platform_compares_paths(tmp_path: Path) -> None:
    config = _config(tmp_path, _servers({"llm-wiki": {"args": [VAULT.upper()]}}))

    expected = "current" if os.path.normcase("A") == os.path.normcase("a") else "elsewhere"
    assert claude_mcp_state(config, VAULT) == expected


def test_both_installers_ask_the_same_helper_and_name_the_unreadable_file() -> None:
    advice = "~/.claude.json could not be read as JSON, so llm-wiki was not registered"
    asked = ["claude-mcp-state --config" in text for text in (INSTALL_SH, INSTALL_PS1)]
    advised = [advice in text for text in (INSTALL_SH, INSTALL_PS1)]

    assert (asked, advised) == ([True, True], [True, True])


@needs_bash
def test_install_sh_reads_a_file_whose_keys_differ_by_case(tmp_path: Path) -> None:
    config = _config(tmp_path, CASE_KEYS.replace(VAULT, str(ROOT)))
    script = f"set -euo pipefail\n{UV_STUB_SH}{_shell_function(INSTALL_SH, 'claude_mcp_state')}\nclaude_mcp_state \"$@\"\n"

    result = subprocess.run(
        [_bash(), "-c", script, "claude_mcp_state", str(config), str(ROOT)],
        capture_output=True, text=True, check=False, timeout=LONG_TIMEOUT,
        env=STUB_ENV,
    )

    assert result.stdout.strip() == "current"


@needs_pwsh
def test_install_ps1_reads_a_file_whose_keys_differ_by_case(tmp_path: Path) -> None:
    config = _config(tmp_path, CASE_KEYS.replace(VAULT, json.dumps(str(ROOT))[1:-1]))
    script = UV_STUB_PS1 + _powershell_functions(ROOT / "install.ps1", ("Get-ClaudeMcpState",)) + (
        f"Get-ClaudeMcpState -Config {ps_literal(str(config))} -VaultRoot {ps_literal(str(ROOT))}\n"
    )

    result = subprocess.run(
        [_pwsh(), "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, check=False, timeout=LONG_TIMEOUT,
        env=STUB_ENV,
    )

    assert result.stdout.strip().splitlines()[-1:] == ["current"]
