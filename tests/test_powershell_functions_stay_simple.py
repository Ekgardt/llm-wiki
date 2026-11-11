"""Measure PowerShell with its native AST, including branch shapes (law 5)."""

from __future__ import annotations

from pathlib import Path

from tests.powershell_functions_stay_simple_helpers import (
    LAW_CEILINGS,
    MEASURE,
    SCRIPT_BODY,
    _measure,
    _problems,
)

ROOT = Path(__file__).resolve().parents[1]
ZONES = ("scripts", "tests", "docs", "skills", "rules", "integrations", "benchmark")
# Script bodies that were over the law-5 ceiling before 2026-11-11, held at what they
# measure now. The owner deferred splitting install.ps1's body to task 178, item 2
# (Pester tests first); an entry goes when its body measures within LAW_CEILINGS.
BODIES_HELD_AT_THEIR_LEVEL = {
    "install.ps1": {"ccn": 86, "ifs": 57, "nesting": 3, "chains": 3, "ternaries": 0},
    "scripts/install-scheduled-tasks.ps1": {"ccn": 21, "ifs": 18, "nesting": 3, "chains": 0, "ternaries": 0},
    "scripts/codex-memory-wrapper.ps1": {"ccn": 8, "ifs": 5, "nesting": 2, "chains": 1, "ternaries": 0},
}


def _shipped_scripts() -> list[Path]:
    return [*ROOT.glob("*.ps1"), *(p for zone in ZONES for p in (ROOT / zone).rglob("*.ps1"))]


def _body_ceiling(path: str) -> dict[str, int]:
    return BODIES_HELD_AT_THEIR_LEVEL.get(Path(path).relative_to(ROOT).as_posix(), LAW_CEILINGS)


def _body_problems(record: dict) -> list[str]:
    return _problems(record, _body_ceiling(record["path"]))


def test_every_powershell_function_obeys_the_laws(tmp_path):
    paths = _shipped_scripts()
    assert ROOT / "install.ps1" in paths
    functions = [record for record in _measure(paths, tmp_path) if record["name"] != SCRIPT_BODY]
    assert [problem for record in functions for problem in _problems(record)] == []


def _bodies(tmp_path) -> dict[str, dict]:
    records = _measure(_shipped_scripts(), tmp_path)
    return {Path(r["path"]).relative_to(ROOT).as_posix(): r for r in records if r["name"] == SCRIPT_BODY}


def _held_level_moved(measured: dict[str, dict], name: str) -> bool:
    held = BODIES_HELD_AT_THEIR_LEVEL[name]
    return {key: measured[name][key] for key in held} != held


def test_no_script_body_grows_past_its_held_level(tmp_path):
    measured = _bodies(tmp_path)
    assert [problem for record in measured.values() for problem in _body_problems(record)] == []
    moved = [name for name in BODIES_HELD_AT_THEIR_LEVEL if _held_level_moved(measured, name)]
    assert moved == [], "a held body improved: lower its entry to what it measures now"


def test_the_measuring_script_obeys_the_laws_it_measures(tmp_path):
    script = tmp_path / "measure-itself.ps1"
    script.write_text(MEASURE, encoding="utf-8")
    assert [problem for record in _measure([script], tmp_path) for problem in _problems(record)] == []


def test_native_measurement_detects_each_violation_and_ignores_nested_function(tmp_path):
    path = tmp_path / "planted.ps1"
    path.write_text("""
function HighCcn { return $a -and $b -or $c -and $d -or $e -and $f }
function ManyIfs { if ($a) {}; if ($b) {}; if ($c) {} }
function Deep { foreach ($x in $xs) { while ($x) { if ($y) {} } } }
function Chain { if ($a) {} elseif ($b) {} }
function Ternary { return $a ? ($b ? 1 : 2) : 3 }
function Outer { function Inner { if ($a) {}; if ($b) {}; if ($c) {} } }
if ($a) {} elseif ($b) {}
foreach ($x in $xs) { if ($x) {} }
""", encoding="utf-8")
    measured = {record["name"]: record for record in _measure([path], tmp_path)}
    assert measured["HighCcn"]["ccn"] == 6
    assert measured["ManyIfs"]["ifs"] == 3
    assert measured["Deep"]["nesting"] == 3
    assert measured["Chain"]["chains"] == 1
    assert measured["Ternary"]["ternaries"] == 1
    assert _problems(measured["Outer"]) == []
    assert all(_problems(measured[name]) for name in ("HighCcn", "ManyIfs", "Deep", "Chain", "Ternary", "Inner"))
    body = measured[SCRIPT_BODY]
    assert (body["ccn"], body["ifs"], body["nesting"], body["chains"]) == (5, 3, 2, 1)
