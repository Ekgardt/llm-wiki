# Tests of `scripts/generation_fts_validation.py`

- `tests/test_generation_fts_validation_refuses_a_damaged_index.py`

`scripts/generation_fts_validation.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_generation_fts_validation_refuses_a_damaged_index.py -q`.
