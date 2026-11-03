"""Every test file has a measured weight, so the CI shards stay even (audit 2026-09-26 C-13).

docs/research/2026-09-26-every-test-file-has-a-weight.md
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests import shard_plan

REPORT = """<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite>
<testcase classname="tests.test_one" name="test_a" time="1.5"/>
<testcase classname="tests.test_one" name="test_b" time="2.0"/>
<testcase classname="" name="tests.test_skipped_whole" time="0.000"/>
</testsuite></testsuites>
"""


def test_every_test_file_has_a_weight_and_every_weight_a_file() -> None:
    files = set(shard_plan.test_files())
    table = set(shard_plan.weights())

    assert (sorted(files - table), sorted(table - files)) == ([], []), (
        "run `python -m tests.shard_plan --weigh <new test files>`"
    )


def test_a_report_counts_each_file_and_a_module_skipped_whole(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    report.write_text(REPORT, encoding="utf-8")

    assert shard_plan.junit_seconds(report) == {"test_one.py": 3.5, "test_skipped_whole.py": 0.0}


def test_the_slowest_job_sets_the_weight(tmp_path: Path) -> None:
    fast, slow = tmp_path / "fast.xml", tmp_path / "slow.xml"
    fast.write_text(REPORT, encoding="utf-8")
    slow.write_text(REPORT.replace('time="2.0"', 'time="9.0"'), encoding="utf-8")

    assert shard_plan.slowest([fast, slow])["test_one.py"] == 10.5


def test_an_unweighted_file_costs_the_table_mean() -> None:
    assert shard_plan._cost("test_new.py", {"a.py": 1.0, "b.py": 9.0}) == 5.0


@pytest.mark.parametrize("measured, expected", [(3.0, 20.0), (27.0, 27.0)])
def test_incremental_measurement_retains_slower_platform_cost(monkeypatch, measured, expected):
    monkeypatch.setattr(shard_plan, "test_files", lambda: ["test_one.py", "test_new.py"])
    actual = shard_plan.refreshed(
        {"test_one.py": 20.0, "test_deleted.py": 9.0},
        {"test_one.py": measured, "test_new.py": 1.25},
    )
    assert actual == {"test_new.py": 1.25, "test_one.py": expected}


def test_weigh_does_not_erase_a_slower_platform_measurement(tmp_path, monkeypatch):
    table = tmp_path / "weights.json"
    table.write_text('{"test_one.py": 20.0}\n', encoding="utf-8")
    monkeypatch.setattr(shard_plan, "WEIGHTS_PATH", table)
    monkeypatch.setattr(shard_plan, "test_files", lambda: ["test_one.py"])
    monkeypatch.setattr(shard_plan, "weigh", lambda _paths: {"test_one.py": 3.0})
    assert shard_plan.main(["--weigh", "tests/test_one.py"]) == 0
    assert shard_plan.weights() == {"test_one.py": 20.0}


def test_explicit_artifact_refresh_can_retire_an_old_slow_cost(tmp_path, monkeypatch):
    table = tmp_path / "weights.json"
    table.write_text('{"test_one.py": 20.0}\n', encoding="utf-8")
    monkeypatch.setattr(shard_plan, "WEIGHTS_PATH", table)
    monkeypatch.setattr(shard_plan, "test_files", lambda: ["test_one.py"])
    (tmp_path / "linux.xml").write_text(REPORT, encoding="utf-8")
    (tmp_path / "windows.xml").write_text(
        REPORT.replace('time="2.0"', 'time="9.0"'), encoding="utf-8",
    )
    assert shard_plan.main(["--refresh", str(tmp_path)]) == 0
    assert shard_plan.weights() == {"test_one.py": 10.5}


@pytest.mark.parametrize("exit_code", [1, 7])
def test_a_failed_measurement_returns_pytest_failure_and_keeps_weights(tmp_path, monkeypatch, exit_code):
    table = tmp_path / "weights.json"
    before = '{"test_one.py": 42.0}\n'
    table.write_text(before, encoding="utf-8")
    monkeypatch.setattr(shard_plan, "WEIGHTS_PATH", table)
    monkeypatch.setattr(shard_plan, "test_files", lambda: ["test_one.py"])

    def failed_pytest(command, **_kwargs):
        report_argument = next(value for value in command if value.startswith("--junitxml="))
        Path(report_argument.split("=", 1)[1]).write_text(REPORT, encoding="utf-8")
        return exit_code

    monkeypatch.setattr(shard_plan.subprocess, "call", failed_pytest)
    result = shard_plan.main(["--weigh", "tests/test_one.py"])
    assert result == exit_code
    assert table.read_text(encoding="utf-8") == before


def test_nested_modules_are_included_but_input_fixtures_are_not(tmp_path, monkeypatch):
    nested = tmp_path / 'modules/formats/test_contract.py'
    fixture = tmp_path / 'fixtures/example/test_input.py'
    for path in (nested, fixture):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('')
    monkeypatch.setattr(shard_plan, 'TESTS_DIR', tmp_path)
    assert shard_plan.test_files() == ['modules/formats/test_contract.py']


def test_junit_preserves_nested_module_path_and_drops_test_class(tmp_path):
    report = tmp_path / 'nested.xml'
    report.write_text(REPORT.replace('tests.test_one', 'tests.modules.formats.test_one.TestContract'))
    assert shard_plan.junit_seconds(report) == {
        'modules/formats/test_one.py': 3.5, 'test_skipped_whole.py': 0.0,
    }
