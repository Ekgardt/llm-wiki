"""Operational observations never certify retained history or authorize deletion."""
import doctor

from tests.test_doctor import _adopt, _build_root


def _unexpected_history_scan(*args, **kwargs):
    raise AssertionError('operational health traversed retained history')


def _check(report, name):
    return {check['id']: check for check in report['checks']}[name]


def test_operational_health_uses_admission_without_history_certification(tmp_path, monkeypatch):
    root, state, home = _build_root(tmp_path)
    _adopt(root, state)
    for name in ('_adoption_check', '_transaction_check', '_queue_check', '_run_deletion_check'):
        monkeypatch.setattr(doctor, name, _unexpected_history_scan)
    report = doctor.run_doctor(root=root, state_root=state, home=home, scope='operational')
    assert report['scope'] == 'operational'
    assert report['coverage']['retained_history'] == 'not_checked'
    assert report['run_deletion']['permit'] is False
    assert report['run_deletion']['quiescent'] is False
    assert not any('retained history' in check['message'] for check in report['checks'])
    assert _check(report, 'adoption')['status'] == 'ok'
    capture = _check(report, 'capture')
    assert capture['details']['adoption_error'] is None


def test_operational_scope_cannot_perform_repairs(tmp_path):
    import pytest

    root, state, home = _build_root(tmp_path)
    with pytest.raises(ValueError, match='full'):
        doctor.run_doctor(root=root, state_root=state, home=home, scope='operational', repair=True)


def test_unknown_health_scope_is_refused(tmp_path):
    import pytest

    root, state, home = _build_root(tmp_path)
    with pytest.raises(ValueError, match='scope'):
        doctor.run_doctor(root=root, state_root=state, home=home, scope='unknown')


def test_partial_operational_report_preserves_completed_failure(monkeypatch):
    import session_start_context as context

    report = {
        'scope': 'operational', 'overall_status': 'error',
        'checks': [
            {'id': 'capture', 'status': 'error', 'message': 'retained capture failure', 'details': {}},
            {'id': 'queue', 'status': 'degraded', 'message': 'not measured', 'details': {'budget_exhausted': True}},
        ],
    }
    monkeypatch.setattr(doctor, 'run_doctor', lambda **kwargs: report)
    block = context._measured_health_block()
    assert 'retained capture failure' in block
    assert '1 of 2' in block
    assert 'operational' in block.lower()


def test_old_healthy_report_does_not_replace_current_measurement(monkeypatch):
    import session_start_context as context

    monkeypatch.setattr(context, '_stored_health_report', lambda: ({'overall_status': 'ok'}, 'yesterday'))
    monkeypatch.setattr(context, '_measured_health_block', lambda: 'current failure')
    assert context.health_block() == 'current failure'
