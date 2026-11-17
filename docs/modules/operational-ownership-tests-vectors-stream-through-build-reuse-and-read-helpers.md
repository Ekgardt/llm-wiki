# Tests of `scripts/operational_ownership.py`

- `tests/vectors_stream_through_build_reuse_and_read_helpers.py`: Helpers shared by test_vectors_stream_through_build_reuse_and_read.py and the test files importing them.

`scripts/operational_ownership.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/vectors_stream_through_build_reuse_and_read_helpers.py -q`.
