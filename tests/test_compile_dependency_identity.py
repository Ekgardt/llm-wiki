"""Declared dependency bytes, settings and origin bind durable compile work."""
import hashlib
import os
import time

import compile_controller as controller
import llm_client as client
import pytest

from tests.test_compile_call_controller import work as work
from tests.test_compile_progress_admission import upgraded as upgraded
from tests.test_compile_protected_dispatch import provider as provider


def _file(tmp_path, name='helper.py', body=b'original'):
    path = tmp_path / name
    path.write_bytes(body)
    return path


def _manifest(path, **parameters):
    return controller.compile_dependency_manifest({'helper': path}, parameters)


def test_changed_helper_bytes_change_identity_without_version_change(tmp_path):
    path = _file(tmp_path)
    original = _manifest(path, version='1')
    metadata = path.stat()
    path.write_bytes(b'modified')
    os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    assert _manifest(path, version='1') != original


def test_manifest_is_order_independent_and_contains_no_configuration_secrets(tmp_path):
    first, second = _file(tmp_path), _file(tmp_path, 'second.py')
    a = controller.compile_dependency_manifest({'a': first, 'b': second}, {'token': 'secret'})
    b = controller.compile_dependency_manifest({'b': second, 'a': first}, {'token': 'secret'})
    assert a == b
    assert 'secret' not in str(a)
    assert a['files']['a']['sha256'] == hashlib.sha256(b'original').hexdigest()
    assert controller.compile_dependency_manifest({'a': first}, {'token': 'secret'}) != a
    assert controller.compile_dependency_manifest({'a': first, 'b': second}, {'token': 'new'}) != a


def test_symlink_dependencies_are_supported_and_resolution_is_bound(tmp_path):
    first, second = _file(tmp_path), _file(tmp_path, 'second.py')
    link = tmp_path / 'linked.py'
    link.symlink_to(first)
    before = _manifest(link)
    link.unlink()
    link.symlink_to(second)
    assert _manifest(link) != before


def test_symlink_change_during_fingerprint_is_refused(tmp_path, monkeypatch):
    first, second = _file(tmp_path), _file(tmp_path, 'second.py')
    link = tmp_path / 'linked.py'
    link.symlink_to(first)
    original = controller.fingerprint_regular_file

    def retarget(*args, **kwargs):
        result = original(*args, **kwargs)
        link.unlink()
        link.symlink_to(second)
        return result

    monkeypatch.setattr(controller, 'fingerprint_regular_file', retarget)
    with pytest.raises(PermissionError, match='resolution changed'):
        _manifest(link)


def test_manifest_missing_dependency_and_expired_deadline_refuse(tmp_path):
    path = _file(tmp_path)
    with pytest.raises(TimeoutError):
        controller.compile_dependency_manifest({'helper': path}, {}, deadline=time.monotonic() - 1)
    path.unlink()
    with pytest.raises(FileNotFoundError):
        _manifest(path)


@pytest.mark.parametrize('field', ['origin_task_id', 'source_digest', 'dependency_digest', 'request'])
def test_work_identity_binds_each_execution_input(field):
    values = dict(origin_task_id='original-task', source_digest='a' * 64,
                  dependency_digest='b' * 64, request={'closed_days_only': True})
    before = controller.compile_work_identity(**values)
    alternatives = dict(origin_task_id='other-task', source_digest='c' * 64,
                        dependency_digest='d' * 64, request={'closed_days_only': False})
    values[field] = alternatives[field]
    assert controller.compile_work_identity(**values) != before


def test_work_identity_rejects_incomplete_identity():
    with pytest.raises(ValueError):
        controller.compile_work_identity('', 'a' * 64, 'b' * 64, {})
    with pytest.raises(ValueError):
        controller.compile_work_identity('task', 'not-a-digest', 'b' * 64, {})


def test_changed_real_dependency_refuses_saved_answer_without_another_call(
    work, provider, tmp_path, monkeypatch,
):
    queue, lease, checkpoint = work
    path = _file(tmp_path)
    calls = []

    def backend(*args):
        calls.append(True)
        return 'answer'

    monkeypatch.setitem(client._BACKENDS, 'fake', backend)

    def run():
        digest = controller.compile_dependency_digest(_manifest(path))
        return controller.run_candidate_call(
            queue, lease, checkpoint, dependency_digest=digest,
            descriptor=provider, prompt='task', system_prompt='system',
        )

    assert run() == run()
    path.write_bytes(b'modified')
    with pytest.raises(ValueError, match='identity'):
        run()
    assert calls == [True]


def _product(tmp_path):
    (tmp_path / 'scripts').mkdir()
    (tmp_path / 'scripts' / 'helper.py').write_text('value = 1')
    (tmp_path / 'pyproject.toml').write_text('[project]\nversion = "1"\n')
    (tmp_path / 'uv.lock').write_text('version = 1\n')
    return tmp_path


def test_product_inventory_notices_added_removed_and_changed_code(tmp_path):
    root = _product(tmp_path)
    before = controller.compile_product_dependency_manifest(root, {})
    extra = root / 'scripts' / 'new.json'
    extra.write_text('{}')
    assert controller.compile_product_dependency_manifest(root, {}) != before
    extra.unlink()
    assert controller.compile_product_dependency_manifest(root, {}) == before
    (root / 'scripts' / 'helper.py').write_text('value = 2')
    assert controller.compile_product_dependency_manifest(root, {}) != before


def test_product_inventory_excludes_only_regenerable_bytecode(tmp_path):
    import importlib.util

    root = _product(tmp_path)
    before = controller.compile_product_dependency_manifest(root, {})
    source = root / 'scripts' / 'helper.py'
    bytecode = type(root)(importlib.util.cache_from_source(str(source)))
    bytecode.parent.mkdir()
    bytecode.write_bytes(b'cache')
    assert controller.compile_product_dependency_manifest(root, {}) == before
    source.unlink()
    after = controller.compile_product_dependency_manifest(root, {})
    assert any(name.endswith('.pyc') for name in after['files'])


def test_product_inventory_supports_directory_links_and_refuses_cycles(tmp_path):
    root = _product(tmp_path)
    shared = root / 'shared'
    shared.mkdir()
    (shared / 'data.json').write_text('{}')
    (root / 'scripts' / 'linked').symlink_to(shared, target_is_directory=True)
    manifest = controller.compile_product_dependency_manifest(root, {})
    assert 'scripts/linked/data.json' in manifest['files']
    (shared / 'cycle').symlink_to(root / 'scripts', target_is_directory=True)
    with pytest.raises(ValueError, match='directory cycle'):
        controller.compile_product_dependency_manifest(root, {})


def _distribution(tmp_path):
    from importlib.metadata import Distribution

    info = tmp_path / 'example-1.dist-info'
    info.mkdir()
    (info / 'METADATA').write_text('Name: example\nVersion: 1\n')
    (info / 'RECORD').write_text('example.py,,\nexample-1.dist-info/METADATA,,\nexample-1.dist-info/RECORD,,\n')
    _file(tmp_path, 'example.py')
    return Distribution.at(info)


def test_package_inventory_hashes_actual_bytes_not_recorded_versions(tmp_path, monkeypatch):
    import importlib.metadata

    dist = _distribution(tmp_path)
    monkeypatch.setattr(importlib.metadata, 'distributions', lambda: [dist])
    before = controller.compile_package_dependency_manifest()
    (tmp_path / 'example.py').write_bytes(b'modified')
    assert controller.compile_package_dependency_manifest() != before
    monkeypatch.setattr(importlib.metadata, 'distributions', lambda: [])
    assert controller.compile_package_dependency_manifest() != before


def test_package_missing_inventory_cannot_claim_complete_identity(tmp_path, monkeypatch):
    import importlib.metadata

    dist = _distribution(tmp_path)
    (tmp_path / 'example-1.dist-info' / 'RECORD').unlink()
    monkeypatch.setattr(importlib.metadata, 'distributions', lambda: [dist])
    with pytest.raises(ValueError, match='inventory'):
        controller.compile_package_dependency_manifest()


def test_editable_package_cannot_hide_uninventoried_external_sources(tmp_path, monkeypatch):
    import importlib.metadata

    dist = _distribution(tmp_path)
    (tmp_path / 'example-1.dist-info' / 'direct_url.json').write_text(
        '{"dir_info":{"editable":true},"url":"file:///private/source"}'
    )
    monkeypatch.setattr(importlib.metadata, 'distributions', lambda: [dist])
    with pytest.raises(ValueError, match='editable'):
        controller.compile_package_dependency_manifest()


def test_expired_package_inventory_does_not_start_discovery(monkeypatch):
    import importlib.metadata

    def forbidden():
        pytest.fail('package discovery started after caller deadline')

    monkeypatch.setattr(importlib.metadata, 'distributions', forbidden)
    with pytest.raises(TimeoutError):
        controller.compile_package_dependency_manifest(deadline=time.monotonic() - 1)
