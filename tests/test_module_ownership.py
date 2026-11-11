"""Context overlap never masquerades as unique whole-project ownership."""
import json
import subprocess

import pytest

from tests.module_ownership import main, ownership_report, tracked_files


def _write(root, name, content=''):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')
    return path


def _manifest(root, owner, files, owned):
    name = f'docs/modules/{owner}-context.json'
    data = {'files': [name, *files], 'ownership': {'files': [name, *owned]}}
    _write(root, name, json.dumps(data))
    return name


def test_complete_registry_has_one_owner_per_file(tmp_path):
    _write(tmp_path, 'source.py')
    manifest = _manifest(tmp_path, 'one', ['source.py'], ['source.py'])
    report = ownership_report(tmp_path, tracked={manifest, 'source.py'})
    assert report['complete'] is True
    assert report['owned_count'] == report['tracked_count'] == 2
    assert report['ownership']['source.py'] == 'one'
    assert report['unassigned'] == []


def test_unassigned_files_remain_visible_even_when_used_as_context(tmp_path):
    _write(tmp_path, 'shared.py')
    manifest = _manifest(tmp_path, 'one', ['shared.py'], [])
    report = ownership_report(tmp_path, tracked={manifest, 'shared.py'})
    assert report['complete'] is False
    assert report['unassigned'] == ['shared.py']


def test_context_may_overlap_but_ownership_may_not(tmp_path):
    _write(tmp_path, 'source.py')
    first = _manifest(tmp_path, 'one', ['source.py'], ['source.py'])
    second = _manifest(tmp_path, 'two', ['source.py'], ['source.py'])
    with pytest.raises(AssertionError, match='Duplicate ownership.*source.py'):
        ownership_report(tmp_path, tracked={first, second, 'source.py'})


def test_untracked_claim_is_refused(tmp_path):
    _write(tmp_path, 'source.py')
    manifest = _manifest(tmp_path, 'one', ['source.py'], ['source.py'])
    with pytest.raises(AssertionError, match='Untracked ownership.*source.py'):
        ownership_report(tmp_path, tracked={manifest})


def test_claim_must_be_in_measured_context(tmp_path):
    _write(tmp_path, 'source.py')
    manifest = _manifest(tmp_path, 'one', [], ['source.py'])
    with pytest.raises(AssertionError, match='Ownership outside context.*source.py'):
        ownership_report(tmp_path, tracked={manifest, 'source.py'})


def test_missing_owned_file_is_refused(tmp_path):
    manifest = _manifest(tmp_path, 'one', ['missing.py'], ['missing.py'])
    with pytest.raises(AssertionError, match='Missing declared context file'):
        ownership_report(tmp_path, tracked={manifest, 'missing.py'})


def test_missing_ownership_declaration_is_refused(tmp_path):
    name = 'docs/modules/one-context.json'
    _write(tmp_path, name, json.dumps({'files': [name]}))
    with pytest.raises(AssertionError, match='Missing ownership declaration'):
        ownership_report(tmp_path, tracked={name})


def test_manifest_must_own_itself(tmp_path):
    name = 'docs/modules/one-context.json'
    _write(tmp_path, name, json.dumps({'files': [name], 'ownership': {'files': []}}))
    with pytest.raises(AssertionError, match='Manifest must own itself'):
        ownership_report(tmp_path, tracked={name})


def test_directory_ownership_is_expanded_and_checked(tmp_path):
    source = _write(tmp_path, 'owned/source.py')
    name = 'docs/modules/one-context.json'
    _write(tmp_path, name, json.dumps({
        'files': [name], 'directories': ['owned'],
        'ownership': {'files': [name], 'directories': ['owned']},
    }))
    report = ownership_report(tmp_path, tracked={name, source.relative_to(tmp_path).as_posix()})
    assert report['ownership']['owned/source.py'] == 'one'
    _write(tmp_path, 'owned/untracked.py')
    with pytest.raises(AssertionError, match='Untracked ownership'):
        ownership_report(tmp_path, tracked={name, 'owned/source.py'})


def test_outside_claim_is_refused(tmp_path):
    name = _manifest(tmp_path, 'one', [], ['../outside.py'])
    with pytest.raises(ValueError):
        ownership_report(tmp_path, tracked={name})


def test_git_inventory_keeps_spaced_names_and_excludes_untracked(tmp_path):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    _write(tmp_path, 'with spaces.py')
    _write(tmp_path, 'untracked.py')
    subprocess.run(['git', '-C', str(tmp_path), 'add', 'with spaces.py'], check=True)
    assert tracked_files(tmp_path) == {'with spaces.py'}


def test_cli_complete_gate_fails_while_partial_report_is_valid(tmp_path, capsys):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    _write(tmp_path, 'unassigned.py')
    subprocess.run(['git', '-C', str(tmp_path), 'add', 'unassigned.py'], check=True)
    assert main(['--root', str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)['complete'] is False
    assert main(['--root', str(tmp_path), '--check-complete']) == 1
    assert json.loads(capsys.readouterr().out)['unassigned'] == ['unassigned.py']


def test_git_inventory_refuses_a_subdirectory_view_of_the_project(tmp_path):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    _write(tmp_path, 'tracked.py')
    subprocess.run(['git', '-C', str(tmp_path), 'add', 'tracked.py'], check=True)
    child = tmp_path / 'empty-subdirectory'
    child.mkdir()
    with pytest.raises(AssertionError, match='repository root'):
        tracked_files(child)


def test_cli_complete_gate_accepts_an_actually_complete_repository(tmp_path, capsys):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    name = _manifest(tmp_path, 'one', [], [])
    subprocess.run(['git', '-C', str(tmp_path), 'add', name], check=True)
    assert main(['--root', str(tmp_path), '--check-complete']) == 0
    assert json.loads(capsys.readouterr().out)['complete'] is True
