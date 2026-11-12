# Tests of `scripts/corpus_snapshot.py`

- `tests/test_a_source_seal_survives_unrelated_directory_entries.py`: Seal unchanged source identity while unrelated sibling names change.

`scripts/corpus_snapshot.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_source_seal_survives_unrelated_directory_entries.py -q`.
