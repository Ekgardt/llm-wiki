"""Python executable, standard library and execution parameters bind replay."""
import sys
import sysconfig
import time
from pathlib import Path

import compile_controller as controller
import pytest
from llm_wiki import compile_controller_dependencies as dependencies


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    # This fixture explicitly models Unix shared/static-library configuration.
    monkeypatch.setattr(sys, 'platform', 'linux')
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
    monkeypatch.setattr(dependencies, 'loaded_python_library', lambda config: library)
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


def test_windows_library_uses_loaded_handle_without_unix_configuration(tmp_path, monkeypatch):
    from llm_wiki import windows_native

    path = tmp_path / 'actual-loaded-runtime.dll'
    path.write_bytes(b'Python DLL')
    handle = 1 << 40
    calls = []

    def filename(value):
        calls.append(value)
        return str(path)

    monkeypatch.setattr(sys, 'platform', 'win32')
    monkeypatch.setattr(sys, 'dllhandle', handle, raising=False)
    monkeypatch.setattr(windows_native, 'loaded_module_filename', filename, raising=False)
    assert controller.compile_runtime_library({}) == {'python-library': path}
    assert calls == [handle]


@pytest.mark.parametrize('handle', [None, 0, -1, True, 'handle'])
def test_windows_library_refuses_missing_or_invalid_handle(monkeypatch, handle):
    monkeypatch.setattr(sys, 'platform', 'win32')
    monkeypatch.setattr(sys, 'dllhandle', handle, raising=False)
    with pytest.raises(ValueError, match='Python DLL handle is unavailable'):
        controller.compile_runtime_library({})


def test_windows_library_propagates_native_lookup_failure(monkeypatch):
    from llm_wiki import windows_native

    def refused(handle):
        raise OSError('native lookup failed')

    monkeypatch.setattr(sys, 'platform', 'win32')
    monkeypatch.setattr(sys, 'dllhandle', 123, raising=False)
    monkeypatch.setattr(windows_native, 'loaded_module_filename', refused)
    with pytest.raises(OSError, match='native lookup failed'):
        controller.compile_runtime_library({})


def test_a_build_machine_libdir_does_not_name_the_library(runtime):
    root, config = runtime
    config['LIBDIR'] = '/install/lib'
    assert controller.compile_runtime_library(config) == {'python-library': root / 'libpython.so.1'}


def _mapped_files():
    return {line.split()[-1] for line in Path('/proc/self/maps').read_text(encoding='utf-8').splitlines()
            if line.split()[-1].startswith('/')}


LINUX_MAPS = pytest.mark.skipif(not Path('/proc/self/maps').is_file(), reason='needs /proc/self/maps')


@LINUX_MAPS
def test_the_named_library_is_the_libpython_this_process_mapped():
    library = dependencies.loaded_python_library(sysconfig.get_config_vars())
    mapped = {path for path in _mapped_files() if Path(path).name.startswith('libpython')}
    assert mapped == (set() if library is None else {str(library.resolve())})


@LINUX_MAPS
def test_a_symbol_is_traced_to_the_shared_object_that_holds_it():
    import ctypes
    import ctypes.util

    libc = ctypes.CDLL(ctypes.util.find_library('c'))
    assert str(dependencies.shared_object_path(libc.getpid).resolve()) in _mapped_files()


@pytest.mark.skipif(sys.platform == 'win32', reason='Windows names the library by its loaded handle')
def test_a_library_this_process_never_loaded_is_not_loaded_to_be_named():
    assert dependencies.loaded_python_library({'INSTSONAME': 'libpython-never-loaded.so.9'}) is None


def test_the_actual_runtime_is_fingerprinted_whatever_its_build_libdir_says():
    config = {**sysconfig.get_config_vars(), 'LIBDIR': '/install/lib'}
    assert all(path.is_file() for path in controller.compile_runtime_library(config).values())


def test_actual_runtime_library_can_be_fingerprinted():
    libraries = controller.compile_runtime_library(sysconfig.get_config_vars())
    for path in libraries.values():
        record = controller.compile_dependency_file(path, None)
        assert record['size'] > 0
        assert len(record['sha256']) == 64
    if sys.platform == 'win32':
        assert set(libraries) == {'python-library'}
