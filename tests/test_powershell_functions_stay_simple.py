"""Measure PowerShell with its native AST, including branch shapes (law 5)."""

from __future__ import annotations

from pathlib import Path

from tests.powershell_functions_stay_simple_helpers import _measure, _problems

ROOT = Path(__file__).resolve().parents[1]
# Native AST distinguishes branches from strings/comments and counts elseif clauses.


def test_every_powershell_function_obeys_the_laws(tmp_path):
    zones = ("scripts", "tests", "docs", "skills", "rules", "integrations", "benchmark")
    paths = [*ROOT.glob("*.ps1"), *(p for zone in zones for p in (ROOT / zone).rglob("*.ps1"))]
    assert ROOT / "install.ps1" in paths
    problems = [problem for record in _measure(paths, tmp_path) for problem in _problems(record)]
    assert problems == []


def test_native_measurement_detects_each_violation_and_ignores_nested_function(tmp_path):
    path = tmp_path / "planted.ps1"
    path.write_text("""
function HighCcn { return $a -and $b -or $c -and $d -or $e -and $f }
function ManyIfs { if ($a) {}; if ($b) {}; if ($c) {} }
function Deep { foreach ($x in $xs) { while ($x) { if ($y) {} } } }
function Chain { if ($a) {} elseif ($b) {} }
function Ternary { return $a ? ($b ? 1 : 2) : 3 }
function Outer { function Inner { if ($a) {}; if ($b) {}; if ($c) {} } }
""", encoding="utf-8")
    measured = {record["name"]: record for record in _measure([path], tmp_path)}
    assert measured["HighCcn"]["ccn"] == 6
    assert measured["ManyIfs"]["ifs"] == 3
    assert measured["Deep"]["nesting"] == 3
    assert measured["Chain"]["chains"] == 1
    assert measured["Ternary"]["ternaries"] == 1
    assert _problems(measured["Outer"]) == []
    assert all(_problems(measured[name]) for name in ("HighCcn", "ManyIfs", "Deep", "Chain", "Ternary", "Inner"))
