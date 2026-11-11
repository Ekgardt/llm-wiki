"""The dependency manifests that bind compile work to the exact product, packages and runtime."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import compile_progress as progress
from llm_wiki.compile_controller_base import compile_identity_digest
from llm_wiki.sqlite_deadlines import operational_deadline_active
from reliable_memory import fingerprint_regular_file


def compile_dependency_file(path, deadline):
    path = Path(path)
    resolved = path.resolve(strict=True)
    digest, identity = fingerprint_regular_file(resolved, resolved.parent, deadline=deadline)
    if path.resolve(strict=True) != resolved:
        raise PermissionError('compile dependency resolution changed during fingerprint')
    return {'sha256': digest, 'size': identity.size,
            'location_digest': compile_identity_digest(str(resolved))}


def compile_dependency_manifest(files, parameters, *, deadline=None):
    """Fingerprint an explicitly inventoried dependency set, not infer its closure.

    Callers own inventory completeness and must rebuild it before accepting replay.
    Only hashes of effective configuration and resolved locations are persisted.
    No file-size ceiling is imposed; stable streaming reads obey caller deadlines.
    """
    operational_deadline_active(deadline)
    configuration = compile_identity_digest(parameters)
    records = {name: compile_dependency_file(path, deadline) for name, path in files.items()}
    manifest = {'schema': 'compile-dependencies/v1', 'files': records,
                'parameters_digest': configuration}
    compile_dependency_digest(manifest)
    return manifest


def compile_dependency_digest(manifest):
    """Bind an inventoried dependency manifest to canonical bytes."""
    return compile_identity_digest(manifest)


def _compile_configuration_identity(root, deadline):
    import model_selection
    import model_selection_file
    import settings
    from llm_client import policy_identity, provider_environment
    from llm_wiki import model_choice
    from model_dlp import load_policy

    operational_deadline_active(deadline)
    environment = provider_environment()
    with model_choice.using_root(root):
        chosen = model_selection.current()
        values = {name: item.value for name, item in settings.effective(root, environment).items()}
        model = None if chosen is None else model_choice.selection_table(chosen)
        policy = policy_identity(load_policy())
        raw_digest = hashlib.sha256(model_selection_file.file_bytes(root)).hexdigest()
    operational_deadline_active(deadline)
    return compile_identity_digest({'root': str(Path(root).resolve(strict=True)),
        'settings_bytes': raw_digest, 'settings': values, 'model': model,
        'provider_environment': environment, 'dlp_policy': policy})


def _require_same_compile_configuration(before, after):
    if before != after:
        raise progress.ProgressConflict('compile configuration changed during inventory')


def compile_configuration_dependency_manifest(root, *, deadline=None):
    """Observe settings, scoped model, validated DLP and the actual child environment.

    Only the resulting digest escapes. Two equal observations reject detected
    concurrent changes; they do not lock noncooperating editors or prevent ABA.
    """
    before = _compile_configuration_identity(root, deadline)
    _require_same_compile_configuration(before, _compile_configuration_identity(root, deadline))
    return {'schema': 'compile-configuration/v1', 'parameters_digest': before}


def compile_execution_dependency_manifest(root, parameters, *, deadline=None):
    """Compose verified inventories without claiming loaded-code or OS closure."""
    configuration = compile_configuration_dependency_manifest(root, deadline=deadline)
    product = compile_product_dependency_manifest(root, parameters, deadline=deadline)
    packages = compile_package_dependency_manifest(deadline=deadline)
    runtime = compile_runtime_dependency_manifest(deadline=deadline)
    _require_same_compile_configuration(configuration,
        compile_configuration_dependency_manifest(root, deadline=deadline))
    return {'schema': 'compile-execution-dependencies/v1', 'configuration': configuration,
            'product': product, 'packages': packages, 'python': runtime}


def capture_compile_dependencies(root, parameters, *, deadline=None):
    """Build a live re-inspected witness from the same execution inventory."""
    inspect = partial(compile_execution_dependency_manifest, root, parameters, deadline=deadline)
    return CompileDependencies.capture(inspect)


def compile_work_identity(origin_task_id, source_digest, dependency_digest, request):
    """Bind execution to its original queue task, retained across explicit redrive."""
    if not isinstance(origin_task_id, str) or not origin_task_id:
        raise ValueError('compile origin task identity must be nonempty text')
    progress.require_digest(source_digest)
    progress.require_digest(dependency_digest)
    return progress.work_identity(origin_task_id, source_digest, dependency_digest,
                                      compile_identity_digest(request))


def _regenerable_dependency_bytecode(path):
    from importlib.util import source_from_cache

    if path.suffix != '.pyc':
        return False
    try:
        return Path(source_from_cache(str(path))).is_file()
    except ValueError:
        return False


def _compile_dependency_tree(directory, ancestors=(), *, deadline=None, excluded=frozenset()):
    operational_deadline_active(deadline)
    resolved = directory.resolve(strict=True)
    if resolved in excluded:
        return
    _require_acyclic_dependency_directory(resolved, ancestors)
    for child in sorted(directory.iterdir()):
        yield from _compile_dependency_entry(child, (*ancestors, resolved), deadline, excluded)
    if directory.resolve(strict=True) != resolved:
        raise PermissionError('compile dependency directory resolution changed')


def _compile_dependency_entry(path, ancestors, deadline, excluded):
    if path.is_dir():
        yield from _compile_dependency_tree(path, ancestors, deadline=deadline, excluded=excluded)
        return
    if not _regenerable_dependency_bytecode(path):
        yield path


def compile_product_dependency_manifest(root, parameters, *, deadline=None):
    """Inventory compiler implementation/data and declared package requirements.

    This product component is not the interpreter or installed-package component.
    Generated bytecode is excluded only when its source still exists. Directory
    symlinks are supported, with cycles refused rather than silently truncated.
    Re-inventory is required at admission: this traversal is not a filesystem lock.
    """
    root = Path(root)
    files = {path.relative_to(root).as_posix(): path
             for path in _compile_dependency_tree(root / 'scripts', deadline=deadline)}
    files.update({name: root / name for name in ('pyproject.toml', 'uv.lock')})
    return compile_dependency_manifest(files, parameters, deadline=deadline)


def _compile_distribution_inventory(distribution):
    direct = json.loads(distribution.read_text('direct_url.json') or '{}')
    if direct.get('dir_info', {}).get('editable'):
        raise ValueError('editable package requires an external source inventory')
    files = distribution.files
    if files is None:
        raise ValueError('installed package has no verifiable file inventory')
    return files


def _compile_distribution_files(distribution, *, deadline=None):
    operational_deadline_active(deadline)
    entries = _compile_distribution_inventory(distribution)
    for entry in entries:
        path = Path(distribution.locate_file(entry))
        if not _regenerable_dependency_bytecode(path):
            yield str(entry), path


def compile_package_dependency_manifest(*, deadline=None):
    """Fingerprint actual declared package bytes, independent of RECORD digests.

    Filesystem distributions with complete inventories are supported here.
    Editable sources need their own inventory; absence is refused, never silently
    treated as a complete package. Interpreter/stdlib and loaded-code identity
    remain separate components of the execution environment.
    """
    import importlib.metadata
    import sys

    operational_deadline_active(deadline)
    files, packages = {}, {}
    for distribution in importlib.metadata.distributions():
        operational_deadline_active(deadline)
        location = str(Path(distribution.locate_file('')).resolve(strict=True))
        key = compile_identity_digest([distribution.metadata['Name'], distribution.version, location])
        packages[key] = {'name': distribution.metadata['Name'], 'version': distribution.version}
        files.update({f'{key}/{name}': path for name, path in
                      _compile_distribution_files(distribution, deadline=deadline)})
    return compile_dependency_manifest(files, {'packages': packages, 'sys_path': sys.path},
                                       deadline=deadline)


@dataclass(frozen=True)
class CompileDependencies:
    """An expected inventory plus a live inspector, never just an unchecked hash.

    The workflow persists digest and reconstructs the inspector on recovery.
    Inventory completeness remains the inspector's contract. Rechecking cannot
    lock noncooperating installers; execution still requires the admission fence.
    """

    digest: str
    inspect: Callable[[], object]

    def __post_init__(self):
        progress.require_digest(self.digest)
        if not callable(self.inspect):
            raise TypeError('compile dependency inspector must be callable')

    @classmethod
    def capture(cls, inspect):
        return cls(compile_dependency_digest(inspect()), inspect)

    def verify(self):
        if compile_dependency_digest(self.inspect()) != self.digest:
            raise progress.ProgressConflict('compile dependency identity changed')
        return self.digest


def _require_acyclic_dependency_directory(resolved, ancestors):
    if resolved in ancestors:
        raise ValueError('compile dependency directory cycle')


def compile_runtime_library(config):
    import sys

    if sys.platform == 'win32':
        return _windows_compile_runtime_library()
    return _unix_compile_runtime_library(config)


def _unix_compile_runtime_library(config):
    if config.get('Py_ENABLE_SHARED') == 0:
        return {}
    if config.get('Py_ENABLE_SHARED') != 1:
        raise ValueError('Python shared-library configuration is unavailable')
    library = loaded_python_library()
    if library is None:
        return {}
    return {'python-library': library}


@dataclass(frozen=True)
class LoadedImage:
    path: Path
    base: int


def loaded_python_library():
    import ctypes

    image = loaded_image(ctypes.cast(ctypes.pythonapi.Py_GetVersion, ctypes.c_void_p).value)
    return library_outside_main_program(image, main_program_base())


def library_outside_main_program(image, main_base):
    if image.base == main_base:
        return None
    if not image.path.is_file():
        raise ValueError(f'the loaded Python library is not a file: {image.path}')
    return image.path


def loaded_image(address):
    import ctypes
    import os

    class SharedObjectInfo(ctypes.Structure):
        _fields_ = [('dli_fname', ctypes.c_char_p), ('dli_fbase', ctypes.c_void_p),
                    ('dli_sname', ctypes.c_char_p), ('dli_saddr', ctypes.c_void_p)]

    dladdr = ctypes.CDLL(None).dladdr
    dladdr.argtypes = [ctypes.c_void_p, ctypes.POINTER(SharedObjectInfo)]
    info = SharedObjectInfo()
    if not dladdr(address, ctypes.byref(info)) or not info.dli_fname or not info.dli_fbase:
        raise ValueError('no loaded image holds the Python runtime')
    return LoadedImage(Path(os.fsdecode(info.dli_fname)), info.dli_fbase)


def main_program_base():
    import sys

    if sys.platform == 'darwin':
        return _darwin_main_program_base()
    if sys.platform.startswith('linux'):
        return _linux_main_program_base()
    raise ValueError(f'the main program image cannot be located on {sys.platform}')


def _linux_main_program_base():
    import ctypes

    getauxval = ctypes.CDLL(None).getauxval
    getauxval.argtypes, getauxval.restype = [ctypes.c_ulong], ctypes.c_ulong
    program_headers = getauxval(3)
    if not program_headers:
        raise ValueError('the kernel reported no program headers for the main program')
    return loaded_image(program_headers).base


def _darwin_main_program_base():
    import ctypes

    header = ctypes.c_char.in_dll(ctypes.CDLL(None), '_mh_execute_header')
    return ctypes.addressof(header)


def _windows_compile_runtime_library():
    import sys

    from llm_wiki.windows_native import loaded_module_filename

    handle = getattr(sys, 'dllhandle', None)
    if isinstance(handle, bool) or not isinstance(handle, int) or handle <= 0:
        raise ValueError('Python DLL handle is unavailable')
    return {'python-library': Path(loaded_module_filename(handle))}


def _compile_runtime_parameters(config):
    import sys

    return {
        'version': sys.version, 'implementation': sys.implementation.name,
        'cache_tag': sys.implementation.cache_tag, 'platform': sys.platform,
        'byteorder': sys.byteorder, 'flags': list(sys.flags), 'xoptions': sys._xoptions,
        'warnoptions': sys.warnoptions, 'sys_path': sys.path,
        'prefixes': [sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix],
        'configuration': config,
    }


def _compile_runtime_tree_layout(paths):
    excluded = frozenset(Path(paths[name]).resolve() for name in ('purelib', 'platlib'))
    roots = sorted({Path(paths[name]) for name in ('stdlib', 'platstdlib')})
    return roots, excluded - frozenset(root.resolve() for root in roots)


def _compile_standard_library_files(paths, deadline):
    roots, excluded = _compile_runtime_tree_layout(paths)
    for root in roots:
        prefix = compile_identity_digest(str(root))
        for path in _compile_dependency_tree(root, deadline=deadline, excluded=excluded):
            yield f'stdlib/{prefix}/{path.relative_to(root).as_posix()}', path


def compile_runtime_dependency_manifest(*, deadline=None):
    """Inventory configured Python files and execution flags, without new limits.

    Package roots are covered by the separate package inventory. This component
    binds on-disk interpreter/stdlib bytes, not old code already loaded in memory
    or transitive operating-system libraries. Unsupported shared-library metadata
    refuses qualification explicitly; no guessed platform pathname is substituted.
    A shared build binds the image that holds the running interpreter's code,
    found by address; an interpreter linked into its executable binds none.
    """
    import sys
    import sysconfig

    operational_deadline_active(deadline)
    paths, config = sysconfig.get_paths(), sysconfig.get_config_vars()
    files = dict(_compile_standard_library_files(paths, deadline))
    files.update(compile_runtime_library(config))
    files['python-executable'] = Path(sys.executable)
    files.update({f'search-path/{index}': Path(path) for index, path in enumerate(sys.path)
                  if Path(path).is_file()})
    return compile_dependency_manifest(files, _compile_runtime_parameters(config), deadline=deadline)
