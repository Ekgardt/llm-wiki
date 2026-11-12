# Tests of `scripts/generation_fts_validation.py`

- `tests/test_generation_validation_streams_sources.py`: Source rederivation must not retain the entire corpus before comparison.

`scripts/generation_fts_validation.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_generation_validation_streams_sources.py -q`.
