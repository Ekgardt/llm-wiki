"""The history is one complete unchanged source, not a selected excerpt."""
from pathlib import Path

import pytest

from tests.module_context import context_reader
from tests.module_records import read_catalog, validated_records

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/breadcrumb-history-context.json')


def _inputs():
    catalog = read_catalog(read_source(ROOT / 'docs/modules/breadcrumb-history-records.json').encode())
    source = read_source(ROOT / catalog['source']).encode()
    return catalog, source


def test_complete_original_reconstruction_and_semantic_boundaries():
    catalog, source = _inputs()
    records = validated_records(catalog, source)
    assert b''.join(records.values()) == source
    assert catalog['bytes'] == 80729
    assert records['overview'].startswith(b'# Breadcrumb durability proposal\n')
    assert records['original-proposal-status'].startswith(b'Status: selected under delegated authority;')
    assert b'Status: selected under delegated authority;' not in records['occurrence-identity-qualification-2026-09-30']
    assert b'generic' in records['history-2026-10-05-generic-breadcrumbs-and-native-identity']


def test_changed_original_tail_is_refused_even_for_earlier_context():
    catalog, source = _inputs()
    with pytest.raises(ValueError, match='digest'):
        validated_records(catalog, source[:-1] + b'X')
