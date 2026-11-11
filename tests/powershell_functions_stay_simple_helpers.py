"""Helpers shared by test_powershell_functions_stay_simple.py and the test files importing them."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

MEASURE = r"""
$ErrorActionPreference = 'Stop'
$paths = @(Get-Content -LiteralPath $args[0] -Raw | ConvertFrom-Json)
$records = @(
foreach ($path in $paths) {
    $tokens = $null
    $errors = $null
    $tree = [System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors)
    if ($errors.Count) { throw ($errors | Out-String) }
    $functions = $tree.FindAll({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst]}, $true)
    foreach ($fn in $functions) {
        $nodes = $fn.Body.FindAll({param($n) $true}, $true)
        $owned = @($nodes | Where-Object {
            $ancestor = $_.Parent
            while ($ancestor -and $ancestor -isnot [System.Management.Automation.Language.FunctionDefinitionAst]) {
                $ancestor = $ancestor.Parent
            }
            $ancestor -eq $fn
        })
        $ccn = 1; $ifs = 0; $nesting = 0; $chains = 0; $ternaries = 0
        foreach ($node in $owned) {
            $kind = $node.GetType().Name
            if ($kind -eq 'IfStatementAst') {
                $ccn += $node.Clauses.Count; $ifs += $node.Clauses.Count
                if ($node.Clauses.Count -gt 1) { $chains++ }
            }
            if ($kind -in @('ForStatementAst', 'ForEachStatementAst', 'WhileStatementAst',
                'DoWhileStatementAst', 'DoUntilStatementAst', 'CatchClauseAst', 'TernaryExpressionAst')) { $ccn++ }
            if ($kind -eq 'SwitchStatementAst') { $ccn += $node.Clauses.Count }
            if ($kind -eq 'BinaryExpressionAst' -and [string]$node.Operator -in @('And', 'Or', 'Xor')) { $ccn++ }
            if ($kind -eq 'PipelineChainAst') { $ccn++ }
            if ($kind -eq 'TernaryExpressionAst') {
                $complex = @($node.FindAll({param($n)
                    $n -is [System.Management.Automation.Language.TernaryExpressionAst] -or
                    ($n -is [System.Management.Automation.Language.BinaryExpressionAst] -and
                        [string]$n.Operator -in @('And', 'Or', 'Xor'))
                }, $true))
                if ($complex.Count -gt 1) { $ternaries++ }
            }
            $depth = 0; $ancestor = $node
            while ($ancestor -and $ancestor -ne $fn) {
                if ($ancestor.GetType().Name -in @('IfStatementAst', 'ForStatementAst',
                    'ForEachStatementAst', 'WhileStatementAst', 'DoWhileStatementAst', 'DoUntilStatementAst')) { $depth++ }
                $ancestor = $ancestor.Parent
            }
            $nesting = [Math]::Max($nesting, $depth)
        }
        [pscustomobject]@{path=$path; name=$fn.Name; ccn=$ccn; ifs=$ifs; nesting=$nesting;
            chains=$chains; ternaries=$ternaries; code=$fn.Extent.Text}
    }
}
)
ConvertTo-Json -InputObject $records -Depth 4 -Compress
"""


def _measure(paths: list[Path], tmp_path: Path) -> list[dict]:
    executable = shutil.which("pwsh")
    if executable is None:
        pytest.skip("PowerShell native parser unavailable; CI requires pwsh")
    script = tmp_path / "measure.ps1"
    script.write_text(MEASURE, encoding="utf-8")
    manifest = tmp_path / "paths.json"
    manifest.write_text(json.dumps([str(path) for path in paths]), encoding="utf-8")
    result = subprocess.run(
        [executable, "-NoProfile", "-NonInteractive", "-File", str(script), str(manifest)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def _problems(record: dict) -> list[str]:
    ceilings = {"ccn": 5, "ifs": 2, "nesting": 2, "chains": 0, "ternaries": 0}
    return [
        f"{record['path']}::{record['name']}: {key}={record[key]} > {bound}\n{record['code']}"
        for key, bound in ceilings.items() if record[key] > bound
    ]
