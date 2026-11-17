"""Helpers shared by test_powershell_functions_stay_simple.py and the test files importing them."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT_BODY = "(script body)"
LAW_CEILINGS = {"ccn": 5, "ifs": 2, "nesting": 2, "chains": 0, "ternaries": 0}
MEASURE = r"""
using namespace System.Management.Automation.Language
$ErrorActionPreference = 'Stop'
$loopKinds = @('ForStatementAst', 'ForEachStatementAst', 'WhileStatementAst', 'DoWhileStatementAst',
    'DoUntilStatementAst')
$branchKinds = @('IfStatementAst') + $loopKinds
$oneDecisionKinds = $loopKinds + @('CatchClauseAst', 'TernaryExpressionAst', 'PipelineChainAst')
function Get-Owner($node) {
    $ancestor = $node.Parent
    while ($ancestor -and $ancestor -isnot [FunctionDefinitionAst]) { $ancestor = $ancestor.Parent }
    return $ancestor
}
function Test-Logical($node) {
    return $node -is [BinaryExpressionAst] -and [string]$node.Operator -in @('And', 'Or', 'Xor')
}
function Get-ClauseCount($node) {
    if ($node -is [IfStatementAst] -or $node -is [SwitchStatementAst]) { return $node.Clauses.Count }
    return 0
}
function Get-Decisions($node) {
    $single = $node.GetType().Name -in $oneDecisionKinds -or (Test-Logical $node)
    return (Get-ClauseCount $node) + [int]$single
}
function Get-IfClauses($node) {
    if ($node -isnot [IfStatementAst]) { return 0 }
    return $node.Clauses.Count
}
function Test-ComplexTernary($node) {
    if ($node -isnot [TernaryExpressionAst]) { return $false }
    $complex = @($node.FindAll({param($n) $n -is [TernaryExpressionAst] -or (Test-Logical $n)}, $true))
    return $complex.Count -gt 1
}
function Get-BranchDepth($node, $unit) {
    $depth = 0; $ancestor = $node
    while ($ancestor -and $ancestor -ne $unit) {
        $depth += [int]($ancestor.GetType().Name -in $branchKinds)
        $ancestor = $ancestor.Parent
    }
    return $depth
}
function Measure-Unit($path, $name, $unit, $root, $code) {
    $owned = @($root.FindAll({param($n) $true}, $true) | Where-Object { $_ -ne $unit -and (Get-Owner $_) -eq $unit })
    $ccn = 1; $ifs = 0; $nesting = 0; $chains = 0; $ternaries = 0
    foreach ($node in $owned) {
        $clauses = Get-IfClauses $node
        $ccn += Get-Decisions $node
        $ifs += $clauses
        $chains += [int]($clauses -gt 1)
        $ternaries += [int](Test-ComplexTernary $node)
        $nesting = [Math]::Max($nesting, (Get-BranchDepth $node $unit))
    }
    [pscustomobject]@{path=$path; name=$name; ccn=$ccn; ifs=$ifs; nesting=$nesting;
        chains=$chains; ternaries=$ternaries; code=$code}
}
$paths = @(Get-Content -LiteralPath $args[0] -Raw | ConvertFrom-Json)
$scriptBody = $args[1]
$records = @(
foreach ($path in $paths) {
    $tokens = $null
    $errors = $null
    $tree = [System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors)
    if ($errors.Count) { throw ($errors | Out-String) }
    $functions = $tree.FindAll({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst]}, $true)
    foreach ($fn in $functions) { Measure-Unit $path $fn.Name $fn $fn.Body $fn.Extent.Text }
    Measure-Unit $path $scriptBody $null $tree '(the statements outside every function)'
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
        [executable, "-NoProfile", "-NonInteractive", "-File", str(script), str(manifest), SCRIPT_BODY],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def _problems(record: dict, ceilings: dict[str, int] = LAW_CEILINGS) -> list[str]:
    return [
        f"{record['path']}::{record['name']}: {key}={record[key]} > {bound}\n{record['code']}"
        for key, bound in ceilings.items() if record[key] > bound
    ]
