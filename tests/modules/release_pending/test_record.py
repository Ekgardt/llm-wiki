"""The pending owner carries the complete Unreleased record and preamble."""
import json
from pathlib import Path

from tests.module_context import context_reader
from tests.module_record_context import selected_entries

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / 'docs/modules/release-pending-context.json'
read_source = context_reader(ROOT, MANIFEST)


def test_pending_context_is_the_complete_original_record():
    manifest = json.loads(read_source(MANIFEST))
    entries = selected_entries(ROOT, manifest)
    assert set(entries) == {'@record/CHANGELOG.md#preamble', '@record/CHANGELOG.md#unreleased'}
    pending = entries['@record/CHANGELOG.md#unreleased']
    assert pending.startswith(b'## [Unreleased]\n')
    assert b'\n## [' not in pending
    assert entries['@record/CHANGELOG.md#preamble'].startswith(b'# Changelog\n')
