# Tests of `scripts/evidence_resolver.py`

- `tests/test_source_choice_reuses_pure_entry_lookup.py`: Repeated resolution must inspect only the canonical entry candidates.

`scripts/evidence_resolver.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_source_choice_reuses_pure_entry_lookup.py -q`.
