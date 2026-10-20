"""Python executable, standard library and execution parameters bind replay."""
import sys
import sysconfig
import time

import compile_controller as controller
import pytest


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    stdlib = tmp_path / 'stdlib'
    stdlib.mkdir()
    packages = stdlib / 'site-packages'
    packages.mkdir()
    (stdlib / 'helper.py').write_text('value = 1')
    (packages / 'external.py').write_text('package component')
    executable = tmp_path / 'python'
    executable.write_bytes(b'interpreter')
    library = tmp_path / 'libpython.so.1'
    library.write_bytes(b'library')
    config = {'Py_ENABLE_SHARED': 1, 'INSTSONAME': library.name, 'LIBDIR': str(tmp_path)}
    paths = dict(stdlib=str(stdlib), platstdlib=str(stdlib), purelib=str(packages), platlib=str(packages))
    monkeypatch.setattr(sys, 'executable', str(executable))
    monkeypatch.setattr(sys, 'path', [])
    monkeypatch.setattr(sysconfig, 'get_paths', lambda: paths)
    monkeypatch.setattr(sysconfig, 'get_config_vars', lambda: config)
    return tmp_path, config


@pytest.mark.parametrize('relative', ['python', 'libpython.so.1', 'stdlib/helper.py'])
def test_changed_runtime_bytes_change_identity_without_version_change(runtime, relative):
    root, _ = runtime
    before = controller.compile_runtime_dependency_manifest()
    (root / relative).write_bytes(b'modified')
    assert controller.compile_runtime_dependency_manifest() != before


def test_package_component_is_not_duplicated_in_stdlib(runtime):
    root, _ = runtime
    before = controller.compile_runtime_dependency_manifest()
    (root / 'stdlib/site-packages/external.py').write_text('updated')
    assert controller.compile_runtime_dependency_manifest() == before
    assert not any('site-packages' in key for key in before['files'])


def test_runtime_configuration_changes_identity(runtime):
    _, config = runtime
    before = controller.compile_runtime_dependency_manifest()
    config['PY_CFLAGS'] = '-DCHANGED'
    assert controller.compile_runtime_dependency_manifest() != before


def test_runtime_archive_on_search_path_is_fingerprinted(runtime, monkeypatch):
    root, _ = runtime
    archive = root / 'python.zip'
    archive.write_bytes(b'archive')
    monkeypatch.setattr(sys, 'path', [str(archive)])
    before = controller.compile_runtime_dependency_manifest()
    archive.write_bytes(b'changed')
    assert controller.compile_runtime_dependency_manifest() != before


def test_missing_declared_shared_library_is_not_silently_ignored(runtime):
    root, _ = runtime
    (root / 'libpython.so.1').unlink()
    with pytest.raises(FileNotFoundError):
        controller.compile_runtime_dependency_manifest()


def test_static_runtime_does_not_require_a_shared_library(runtime):
    root, config = runtime
    config['Py_ENABLE_SHARED'] = 0
    (root / 'libpython.so.1').unlink()
    assert controller.compile_runtime_dependency_manifest()['files']


def test_expired_runtime_inventory_does_not_discover_paths(runtime, monkeypatch):
    monkeypatch.setattr(sysconfig, 'get_paths', lambda: pytest.fail('discovery after deadline'))
    with pytest.raises(TimeoutError):
        controller.compile_runtime_dependency_manifest(deadline=time.monotonic() - 1)


def test_overlapping_stdlib_and_package_roots_do_not_hide_standard_code(runtime, monkeypatch):
    root, _ = runtime
    shared = str(root / 'stdlib')
    monkeypatch.setattr(sysconfig, 'get_paths', lambda: dict.fromkeys(
        ('stdlib', 'platstdlib', 'purelib', 'platlib'), shared,
    ))
    before = controller.compile_runtime_dependency_manifest()
    (root / 'stdlib/helper.py').write_text('value = 2')
    assert controller.compile_runtime_dependency_manifest() != before


def test_unknown_shared_library_configuration_refuses_qualification(runtime):
    _, config = runtime
    config.pop('Py_ENABLE_SHARED')
    with pytest.raises(ValueError, match='configuration is unavailable'):
        controller.compile_runtime_dependency_manifest()
