"""Both installers register the Claude MCP entry and report it the same way (audit 2026-09-26 C-13).

docs/research/2026-09-26-the-installers-register-claude-mcp-alike.md
"""
from __future__ import annotations

import json
import re
import subprocess

from tests.test_the_installer_says_what_it_needs import (
    INSTALL_PS1,
    INSTALL_SH,
    ROOT,
    _call,
    _powershell_functions,
    _pwsh,
    needs_bash,
    needs_pwsh,
)

STATES = ("current", "elsewhere", "absent", "unreadable")
_STATUS = re.compile(r'"(Claude Code: [^"$]*)"')


def test_every_status_line_install_sh_prints_install_ps1_prints_too() -> None:
    missing = set(_STATUS.findall(INSTALL_SH)) - set(_STATUS.findall(INSTALL_PS1))

    assert missing == set()


def test_both_installers_try_the_claude_cli_before_asking_the_operator() -> None:
    command = "claude mcp add --scope user llm-wiki -- uv run --locked --no-sync --directory"

    assert [command in text for text in (INSTALL_SH, INSTALL_PS1)] == [True, True]


@needs_bash
@needs_pwsh
def test_each_entry_state_reads_the_same_in_both_installers() -> None:
    sh_lines = [_call("claude_status_line", state).stdout.strip() for state in STATES]
    script = _powershell_functions(ROOT / "install.ps1", ("Get-ClaudeStatusLine",)) + (
        "ConvertTo-Json -Compress @("
        + ", ".join(f'(Get-ClaudeStatusLine -Automatic $true -McpState "{state}")' for state in STATES)
        + ")\n"
    )

    result = subprocess.run(
        [_pwsh(), "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=120, check=False,
    )

    assert json.loads(result.stdout.splitlines()[-1]) == sh_lines
