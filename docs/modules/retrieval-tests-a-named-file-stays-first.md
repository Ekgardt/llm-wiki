# Tests of `scripts/retrieval.py`

- `tests/test_a_named_file_stays_first.py`: A file asked for by its own name is the first result, whatever kind of file it is.

`scripts/retrieval.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_named_file_stays_first.py -q`.
