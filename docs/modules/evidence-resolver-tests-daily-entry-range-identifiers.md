# Tests of `scripts/evidence_resolver.py`

- `tests/test_daily_entry_range_identifiers.py`: Canonical entry IDs preserve old text semantics without whole-entry copies.

`scripts/evidence_resolver.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_daily_entry_range_identifiers.py -q`.
