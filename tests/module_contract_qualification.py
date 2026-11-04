"""Run real owned tests now; never treat a cached success as qualification."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from tests.module_contract_environment import environment_identity
from tests.module_contract_spec import pilot_inputs
from tests.slow_machine import LONG_TIMEOUT


def require_observed_tests(report: dict, selected: list[str]) -> None:
    _require_execution_success(report)
    collected = report['collected']
    if not collected or set(collected.values()) != set(selected):
        raise ValueError('Observed test files differ from the complete selection')
    _require_test_phases(report['reports'], set(collected))


def _require_execution_success(report: dict) -> None:
    if report['exitstatus'] or report['deselected'] or report['violations']:
        raise ValueError('Test execution failed, was filtered, or violated runtime bindings')


def _require_test_phases(reports: list[dict], collected: set[str]) -> None:
    seen = _observed_phases(reports)
    expected = {(node, phase) for node in collected for phase in ('setup', 'call', 'teardown')}
    if seen != expected:
        raise ValueError('Missing or unexpected test phase evidence')


def _observed_phases(reports: list[dict]) -> set[tuple[str, str]]:
    seen = set()
    for report in reports:
        _require_pass(report)
        key = (report['nodeid'], report['when'])
        if key in seen:
            raise ValueError('Duplicate test phase evidence')
        seen.add(key)
    return seen


def _require_pass(report: dict) -> None:
    if report['outcome'] != 'passed' or report['xfail']:
        raise ValueError('Skipped, failed or expected-failure tests cannot qualify')


def _require_junit(path: Path, count: int) -> None:
    tree = ET.parse(path)
    cases = tree.findall('.//testcase')
    if len(cases) != count:
        raise ValueError('JUnit and runtime observation disagree')
    if any(list(case) for case in cases):
        raise ValueError('JUnit reports a failure, skip or unexpected test evidence')


def _command(plan: Path, selected: list[str]) -> list[str]:
    return [sys.executable, '-X', f'pycache_prefix={plan.parent / "bytecode"}', '-B',
            '-m', 'pytest', '-p', 'tests.module_contract_probe',
            f'--dependency-contract-plan={plan}', f'--junitxml={plan.parent / "tests.xml"}',
            '-q', *selected]


def _execute(root: Path, plan: Path, selected: list[str]) -> None:
    if os.environ.get('PYTEST_ADDOPTS', '').strip():
        raise ValueError('Ambient pytest options would make this experiment ambiguous')
    with plan.with_name('pytest.txt').open('xb') as output:
        result = subprocess.run(_command(plan, selected), cwd=root, stdout=output,
                                stderr=subprocess.STDOUT, timeout=LONG_TIMEOUT)
    if result.returncode:
        raise ValueError(f'Fresh tests failed: {plan.with_name("pytest.txt")}')


def _qualification_inputs(root: Path, pilot: Path) -> dict:
    return {**pilot_inputs(root, pilot), 'environment': environment_identity()}


def qualify(root: Path, pilot: Path, output: Path) -> dict:
    """An experiment record is descriptive only; each invocation executes tests."""
    root = root.resolve()
    before = _qualification_inputs(root, pilot)
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    plan = output / 'plan.json'
    plan.write_text(json.dumps({'root': str(root), **before}, indent=2), encoding='utf-8')
    _execute(root, plan, before['tests'])
    report = json.loads((output / 'probe-result.json').read_bytes())
    require_observed_tests(report, before['tests'])
    _require_junit(output / 'tests.xml', len(report['collected']))
    after = _qualification_inputs(root, pilot)
    if before != after:
        raise ValueError('Inputs changed during qualification; a new experiment is required')
    result = {'status': 'experiment-passed-not-adopted', 'inputs': before, 'execution': report}
    (output / 'experiment.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    return result
