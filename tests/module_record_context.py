"""Load reviewed record catalogs without path aliases or hidden source changes."""
from pathlib import Path, PurePosixPath

from tests.module_context_paths import _contained
from tests.module_records import _fields, _require, read_catalog, record_entries, validated_records

_RECORD_TOOLS = (
    'tests/module_records.py', 'tests/module_record_context.py',
    'tests/module_context_paths.py', 'docs/modules/context-records.md',
)


def record_selections(manifest):
    selections = manifest.get('records', [])
    _require(isinstance(selections, list), 'Record selections must be a list')
    for selection in selections:
        _fields(selection, {'catalog', 'ids'})
    return selections


def _named_path(root: Path, name: str) -> Path:
    _require(isinstance(name, str) and bool(name), 'Record path must be named')
    logical = PurePosixPath(name)
    _require(bool(logical.parts), 'Record path must name a file')
    _require('..' not in logical.parts and '\\' not in name, 'Record path escapes its name')
    _require(not logical.is_absolute() and logical.as_posix() == name, 'Record path must be canonical relative')
    path = _contained(root, name)
    _require(path == root.resolve() / name, 'Record path resolves through an alias')
    return path


def load_catalog(root: Path, name: str):
    path = _named_path(root, name)
    catalog = read_catalog(path.read_bytes())
    source_path = _named_path(root, catalog['source'])
    source = source_path.read_bytes()
    validated_records(catalog, source)
    return path, catalog, source


def context_record_files(root: Path, manifest: dict) -> set[Path]:
    selections = record_selections(manifest)
    if not selections:
        return set()
    catalogs = {_named_path(root, item['catalog']) for item in selections}
    return catalogs | {root.resolve() / name for name in _RECORD_TOOLS}


def selected_entries(root: Path, manifest: dict) -> dict[str, bytes]:
    entries = {}
    for selection in record_selections(manifest):
        _path, catalog, source = load_catalog(root, selection['catalog'])
        selected = record_entries(catalog, source, selection['ids'])
        _require(entries.keys().isdisjoint(selected), 'Duplicate record context entry')
        entries.update(selected)
    return entries
