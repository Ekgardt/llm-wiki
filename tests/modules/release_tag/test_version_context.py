"""Shared historical data is the actual current version's complete section."""
import json
from pathlib import Path

import release_tag

from tests.module_context import context_reader
from tests.module_record_context import load_catalog, selected_entries

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / 'docs/modules/release-tag-context.json'
read_source = context_reader(ROOT, MANIFEST)


def test_selected_section_is_bound_to_actual_project_version():
    manifest = json.loads(read_source(MANIFEST))
    version = release_tag.project_version(read_source(ROOT / 'pyproject.toml'))
    entries = selected_entries(ROOT, manifest)
    key = '@record/CHANGELOG.md#v' + version.replace('.', '-')
    assert set(entries) == {'@record/CHANGELOG.md#preamble', key}
    _path, _catalog, source = load_catalog(ROOT, 'docs/modules/release-history-records.json')
    section = entries[key].decode('utf-8')
    assert section.startswith(f'## [{version}]')
    assert release_tag.changelog_section(section, version) == release_tag.changelog_section(source.decode('utf-8'), version)
