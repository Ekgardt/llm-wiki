"""Helpers shared by test_installer_bootstrap.py and the test files importing them."""
from __future__ import annotations

import json
import os
import re
import shutil
import textwrap
from dataclasses import dataclass
from pathlib import Path

from tests.powershell_literal import ps_literal


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


def _bash() -> str | None:
    if os.name == "nt":
        return None
    return shutil.which("bash")
