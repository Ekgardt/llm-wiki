# Tests of `scripts/generation_search.py`

- `tests/test_vectors_stream_through_build_reuse_and_read.py`: Streaming vectors must survive all three consumers of the same sealed format.

`scripts/generation_search.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_vectors_stream_through_build_reuse_and_read.py -q`.
