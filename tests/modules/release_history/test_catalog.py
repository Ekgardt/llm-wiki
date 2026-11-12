"""Reviewed release records cover the unchanged original source exactly."""
import re
from pathlib import Path

from tests.module_record_context import load_catalog
from tests.module_records import validated_records

ROOT = Path(__file__).resolve().parents[3]
CATALOG = 'docs/modules/release-history-records.json'


def test_every_original_byte_is_reconstructed():
    _path, catalog, source = load_catalog(ROOT, CATALOG)
    assert b''.join(validated_records(catalog, source).values()) == source


def _catalog_headings():
    _path, catalog, source = load_catalog(ROOT, CATALOG)
    headings = list(re.finditer(rb'^## \[([^\]]+)\].*$', source, re.MULTILINE))
    return catalog, headings


def test_records_follow_every_real_version_heading():
    catalog, headings = _catalog_headings()
    assert [row['start'] for row in catalog['records']] == [0, *[h.start() for h in headings]]
    assert len({h[1] for h in headings}) == len(headings)


def test_catalog_labels_are_the_actual_version_headings():
    catalog, headings = _catalog_headings()
    assert [row['label'] for row in catalog['records'][1:]] == [h[0].decode('utf-8') for h in headings]


def test_current_boundary_review_has_no_unhandled_markdown_scope():
    _path, _catalog, source = load_catalog(ROOT, CATALOG)
    assert not re.search(rb'^ {0,3}(`{3,}|~{3,})', source, re.MULTILINE), 'Review fenced block scope'
    assert not re.search(rb'^ {0,3}\[[^\]]+\]:', source, re.MULTILINE), 'Review reference-definition scope'
