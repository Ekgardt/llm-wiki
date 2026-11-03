"""Partial record ownership cannot claim the entire original artifact."""
import json

import pytest

from tests.module_ownership import ownership_report
from tests.test_module_record_context import workspace as workspace
from tests.test_module_records import catalog as catalog


def _owner(root, name, ids, *, claims=None, whole=False):
    path = f'docs/modules/{name}-context.json'
    files = [path, 'docs/records.json']
    owned = [path]
    if name == 'one':
        owned.append('docs/records.json')
    if whole:
        files.append('docs/history.md')
        owned.append('docs/history.md')
    selection = [{'catalog': 'docs/records.json', 'ids': ids}]
    declared = selection if claims is None else claims
    data = {'files': files, 'records': selection,
            'ownership': {'files': owned, 'records': declared}}
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data))
    return path


def _tracked(*manifests):
    return {'docs/history.md', 'docs/records.json', *manifests}


def test_partial_record_ownership_keeps_source_unassigned(workspace):
    root, _ = workspace
    one = _owner(root, 'one', ['first'])
    report = ownership_report(root, tracked=_tracked(one))
    assert report['unassigned'] == ['docs/history.md']
    assert report['partially_owned'] == {'docs/history.md': ['second']}
    assert report['record_ownership'] == {'@record/docs/history.md#first': 'one'}
    assert not report['complete']


def test_all_records_need_exactly_one_owner_before_source_is_complete(workspace):
    root, _ = workspace
    one = _owner(root, 'one', ['first'])
    two = _owner(root, 'two', ['second'])
    report = ownership_report(root, tracked=_tracked(one, two))
    assert report['schema_version'] == 'module-ownership/v2'
    assert report['complete']
    assert report['fully_recorded'] == ['docs/history.md']
    assert report['owned_count'] == report['tracked_count'] == 4
    assert report['partially_owned'] == {}
    assert report['record_ownership']['@record/docs/history.md#second'] == 'two'


def test_duplicate_record_owner_fails(workspace):
    root, _ = workspace
    one = _owner(root, 'one', ['first'])
    two = _owner(root, 'two', ['first'])
    with pytest.raises(ValueError, match='Duplicate record ownership'):
        ownership_report(root, tracked=_tracked(one, two))


def test_whole_and_record_ownership_cannot_overlap(workspace):
    root, _ = workspace
    one = _owner(root, 'one', ['first'], whole=True)
    with pytest.raises(ValueError, match='Whole-file and record ownership overlap'):
        ownership_report(root, tracked=_tracked(one))


def test_owned_record_must_be_in_measured_selection(workspace):
    root, _ = workspace
    claims = [{'catalog': 'docs/records.json', 'ids': ['second']}]
    one = _owner(root, 'one', ['first'], claims=claims)
    with pytest.raises(ValueError, match='outside measured selection'):
        ownership_report(root, tracked=_tracked(one))


def test_untracked_original_source_cannot_gain_record_ownership(workspace):
    root, _ = workspace
    one = _owner(root, 'one', ['first'])
    with pytest.raises(ValueError, match='Untracked record source'):
        ownership_report(root, tracked=_tracked(one) - {'docs/history.md'})


def test_competing_catalogs_for_one_source_fail(workspace):
    root, _ = workspace
    one = _owner(root, 'one', ['first'])
    two = _owner(root, 'two', ['second'])
    alternate = root / 'docs/alternative.json'
    alternate.write_bytes((root / 'docs/records.json').read_bytes())
    path = root / two
    data = json.loads(path.read_bytes())
    data['records'][0]['catalog'] = 'docs/alternative.json'
    data['ownership']['records'][0]['catalog'] = 'docs/alternative.json'
    data['files'].append('docs/alternative.json')
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='Competing record catalogs'):
        ownership_report(root, tracked=_tracked(one, two) | {'docs/alternative.json'})


def test_changed_unowned_record_invalidates_ownership(workspace):
    root, _ = workspace
    one = _owner(root, 'one', ['first'])
    (root / 'docs/history.md').write_text('changed')
    with pytest.raises(ValueError, match='length changed'):
        ownership_report(root, tracked=_tracked(one))


def test_context_records_do_not_make_a_whole_owned_source_partial(workspace):
    root, _ = workspace
    one = _owner(root, 'one', ['first'], claims=[], whole=True)
    report = ownership_report(root, tracked=_tracked(one))
    assert report['complete']
    assert report['partially_owned'] == {}
    assert report['fully_recorded'] == []
    assert report['record_ownership'] == {}
