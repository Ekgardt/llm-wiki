"""A report for one marker environment cannot certify the universal lock."""
import json

import pytest
from check_dependency_audit import locked_pairs, main, verify_coverage


def _package(version, **changes):
    return {'name': 'example', 'version': version,
            'source': {'registry': 'https://example.invalid/simple'}, **changes}


def _report(*versions):
    return {'dependencies': [{'name': 'example', 'version': v, 'vulns': []} for v in versions]}


def test_mutually_exclusive_marker_versions_both_require_audit():
    lock = {'package': [
        _package('1.0', **{'resolution-markers': ["python_full_version < '3.11'"]}),
        _package('2.0', **{'resolution-markers': ["python_full_version >= '3.11'"]}),
    ]}
    with pytest.raises(ValueError, match='missing=.*1.0'):
        verify_coverage(lock, _report('2.0'))
    assert verify_coverage(lock, _report('1.0', '2.0')) == 2


def test_different_platform_and_local_build_remain_exact_versions():
    lock = {'package': [_package('1.0+cpu'), _package('1.0', **{'resolution-markers': ["sys_platform == 'win32'"]})]}
    assert verify_coverage(lock, _report('1.0', '1.0+cpu')) == 2
    with pytest.raises(ValueError, match='missing='):
        verify_coverage(lock, _report('1.0'))


@pytest.mark.parametrize('source', [{'virtual': '.'}, {'editable': '.'}])
def test_only_local_project_is_excluded(source):
    lock = {'package': [_package('1.0'), _package('0.1', name='local', source=source)]}
    assert verify_coverage(lock, _report('1.0')) == 1


@pytest.mark.parametrize('source', [
    {'git': 'https://example.invalid/repo'}, {'virtual': '../other'},
    {'editable': 'subproject'}, {'url': 'https://example.invalid/wheel'}, {},
])
def test_unknown_source_is_visible_failure(source):
    with pytest.raises(ValueError, match='Unsupported locked source'):
        locked_pairs({'package': [_package('1.0', source=source)]})


@pytest.mark.parametrize('packages', [[], [_package('1.0', source={'virtual': '.'})]])
def test_empty_external_inventory_does_not_pass(packages):
    with pytest.raises(ValueError, match='No registry dependencies'):
        verify_coverage({'package': packages}, _report())


@pytest.mark.parametrize('record', [
    {'name': 'example', 'skip_reason': 'unavailable'},
    {'name': 'example', 'version': '1.0', 'vulns': [{'id': 'EXAMPLE-1'}]},
    {'name': 'example', 'vulns': []}, {'name': '', 'version': '1.0', 'vulns': []},
    {'name': None, 'version': '1.0', 'vulns': []}, {'name': 'example', 'version': 1, 'vulns': []},
    {'name': 'example', 'version': '1.0'},
    {'name': 'example', 'version': '1.0', 'vulns': None},
])
def test_skips_findings_and_incomplete_identities_cannot_claim_coverage(record):
    with pytest.raises(ValueError):
        verify_coverage({'package': [_package('1.0')]}, {'dependencies': [record]})


def test_unexpected_report_version_is_rejected():
    with pytest.raises(ValueError, match='extra=.*2.0'):
        verify_coverage({'package': [_package('1.0')]}, _report('1.0', '2.0'))


def test_duplicate_identical_records_do_not_inflate_coverage():
    assert verify_coverage({'package': [_package('1.0'), _package('1.0')]}, _report('1.0', '1.0')) == 1


def test_cli_reads_utf8_and_reports_coverage(tmp_path, capsys):
    lock = tmp_path / 'uv.lock'
    lock.write_text('# проверка\n[[package]]\nname = "example"\nversion = "1.0"\nsource = { registry = "https://example.invalid" }\n', encoding='utf-8')
    report = tmp_path / 'report.json'
    report.write_text(json.dumps(_report('1.0')), encoding='utf-8')
    assert main([str(lock), str(report)]) == 0
    assert json.loads(capsys.readouterr().out) == {'status': 'covered', 'locked_versions': 1}
