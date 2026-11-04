"""Missing or filtered runtime evidence cannot masquerade as a successful run."""
import copy
import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.module_contract_qualification import (
    _command,
    _require_junit,
    require_observed_tests,
)
from tests.slow_machine import LONG_TIMEOUT


def _report():
    return {'exitstatus': 0, 'deselected': [], 'violations': [],
            'collected': {'test_example.py::test_real': 'test_example.py'},
            'reports': [{'nodeid': 'test_example.py::test_real', 'when': phase,
                         'outcome': 'passed', 'xfail': False}
                        for phase in ('setup', 'call', 'teardown')]}


def test_complete_observed_phases_are_required():
    require_observed_tests(_report(), ['test_example.py'])


@pytest.mark.parametrize(('field', 'value'), [
    ('exitstatus', 1), ('deselected', ['omitted']), ('violations', ['wrong binding']),
    ('collected', {}), ('collected', {'other': 'other.py'}), ('reports', []),
])
def test_incomplete_or_failed_execution_refuses(field, value):
    report = _report()
    report[field] = value
    with pytest.raises(ValueError):
        require_observed_tests(report, ['test_example.py'])


@pytest.mark.parametrize('phase', ['setup', 'call', 'teardown'])
@pytest.mark.parametrize('outcome', ['skipped', 'failed'])
def test_every_phase_must_really_pass(phase, outcome):
    report = _report()
    selected = next(item for item in report['reports'] if item['when'] == phase)
    selected['outcome'] = outcome
    with pytest.raises(ValueError):
        require_observed_tests(report, ['test_example.py'])


def test_expected_failure_is_not_an_unqualified_pass():
    report = _report()
    report['reports'][1]['xfail'] = True
    with pytest.raises(ValueError, match='expected-failure'):
        require_observed_tests(report, ['test_example.py'])


def test_duplicate_phase_cannot_replace_missing_phase():
    report = _report()
    report['reports'][2] = copy.deepcopy(report['reports'][1])
    with pytest.raises(ValueError, match='Duplicate'):
        require_observed_tests(report, ['test_example.py'])


def test_a_missing_owned_file_refuses():
    with pytest.raises(ValueError, match='complete selection'):
        require_observed_tests(_report(), ['test_example.py', 'test_missing.py'])


@pytest.mark.parametrize('detail', ['<failure/>', '<error/>', '<skipped/>'])
def test_junit_failure_cannot_be_hidden_by_probe(tmp_path, detail):
    path = tmp_path / 'tests.xml'
    path.write_text(f'<testsuites><testsuite><testcase>{detail}</testcase></testsuite></testsuites>',
                    encoding='utf-8')
    with pytest.raises(ValueError, match='JUnit reports'):
        _require_junit(path, 1)


def test_junit_count_must_agree(tmp_path):
    path = tmp_path / 'tests.xml'
    path.write_text('<testsuites><testsuite><testcase/></testsuite></testsuites>', encoding='utf-8')
    _require_junit(path, 1)
    with pytest.raises(ValueError, match='disagree'):
        _require_junit(path, 2)


def test_fresh_bytecode_and_explicit_real_test_selection():
    plan = Path('/experiment/plan.json')
    command = _command(plan, ['tests/test_real.py'])
    assert command[1:4] == ['-X', f'pycache_prefix={plan.parent / "bytecode"}', '-B']
    assert command[-1] == 'tests/test_real.py'
    assert '--dependency-contract-plan=' + str(plan) in command


def _probe_checkout(tmp_path, test_source):
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    (scripts / 'contract_probe_provider.py').write_text('def count(value):\n    return len(value)\n',
                                                       encoding='utf-8')
    (scripts / 'contract_probe_consumer.py').write_text('from contract_probe_provider import count\n',
                                                       encoding='utf-8')
    (tmp_path / 'pyproject.toml').write_text('[tool.pytest.ini_options]\npythonpath=["scripts"]\n',
                                            encoding='utf-8')
    (tmp_path / 'test_probe.py').write_text(test_source, encoding='utf-8')
    spec = {'provider_module': 'contract_probe_provider', 'consumer_module': 'contract_probe_consumer',
            'provider_source': 'scripts/contract_probe_provider.py',
            'consumer_source': 'scripts/contract_probe_consumer.py', 'bindings': {'count': 'count'}}
    plan = tmp_path / 'plan.json'
    plan.write_text(json.dumps({'root': str(tmp_path), 'spec': spec}), encoding='utf-8')
    return plan


def _run_probe(tmp_path, source):
    plan = _probe_checkout(tmp_path, source)
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    return subprocess.run(_command(plan, ['test_probe.py']), cwd=tmp_path,
                          env=environment, capture_output=True, text=True, timeout=LONG_TIMEOUT)


def test_real_pytest_observer_records_all_phases_and_restored_injection(tmp_path):
    result = _run_probe(tmp_path, '''import contract_probe_consumer as consumer
def test_real(monkeypatch):
    assert consumer.count('abc') == 3
    monkeypatch.setattr(consumer, 'count', lambda value: 9)
    assert consumer.count('abc') == 9
''')
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((tmp_path / 'probe-result.json').read_bytes())
    require_observed_tests(report, ['test_probe.py'])


def test_real_pytest_observer_rejects_an_unrestored_binding(tmp_path):
    result = _run_probe(tmp_path, '''import contract_probe_consumer as consumer
def test_real():
    consumer.count = lambda value: 9
''')
    assert result.returncode != 0
    assert 'Changed public binding' in result.stdout + result.stderr


def test_real_pytest_skip_does_not_qualify(tmp_path):
    result = _run_probe(tmp_path, '''import pytest
@pytest.mark.skip(reason='not actual execution')
def test_real():
    assert False
''')
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((tmp_path / 'probe-result.json').read_bytes())
    with pytest.raises(ValueError, match='Skipped'):
        require_observed_tests(report, ['test_probe.py'])


def test_real_pytest_observer_refuses_a_substituted_fixture(tmp_path):
    result = _run_probe(tmp_path, '''import pytest
@pytest.fixture
def monkeypatch():
    return object()
def test_real(monkeypatch):
    pass
''')
    assert result.returncode != 0
    assert 'Substituted monkeypatch fixture' in result.stdout + result.stderr


def test_environment_byte_change_during_execution_refuses(tmp_path, monkeypatch):
    """Unit probe isolates the rejection gate; it is not provider qualification."""
    from tests import module_contract_qualification as runner
    from tests.module_contract_environment import _tree

    environment = tmp_path / 'environment'
    environment.mkdir()
    dependency = environment / 'installed.py'
    dependency.write_bytes(b'old source')
    monkeypatch.setattr(runner, 'pilot_inputs', lambda *_: {'tests': ['test_example.py']})
    monkeypatch.setattr(runner, 'environment_identity', lambda: _tree(environment))

    def execute(_root, plan, _selected):
        (plan.parent / 'probe-result.json').write_text(json.dumps(_report()), encoding='utf-8')
        (plan.parent / 'tests.xml').write_text(
            '<testsuites><testsuite><testcase/></testsuite></testsuites>', encoding='utf-8')
        dependency.write_bytes(b'new source')

    monkeypatch.setattr(runner, '_execute', execute)
    with pytest.raises(ValueError, match='Inputs changed'):
        runner.qualify(tmp_path, tmp_path / 'pilot.json', tmp_path / 'output')
    assert not (tmp_path / 'output/experiment.json').exists()
