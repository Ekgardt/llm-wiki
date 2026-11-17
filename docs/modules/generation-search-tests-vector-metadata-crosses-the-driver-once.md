# Tests of `scripts/generation_search.py`

- `tests/test_vector_metadata_crosses_the_driver_once.py`: All identities are checked without repeated Python/SQLite crossings.

`scripts/generation_search.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_vector_metadata_crosses_the_driver_once.py -q`.
