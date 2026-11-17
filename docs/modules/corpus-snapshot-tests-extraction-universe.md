# Tests of `scripts/corpus_snapshot.py`

- `tests/test_extraction_universe.py`: Why `extract_code` batches the whole universe, pinned so it stays true.

`scripts/corpus_snapshot.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_extraction_universe.py -q`.
