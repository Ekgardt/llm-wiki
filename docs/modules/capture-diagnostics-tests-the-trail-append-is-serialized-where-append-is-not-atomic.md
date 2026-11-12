# Tests of `scripts/capture_diagnostics.py`

- `tests/test_the_trail_append_is_serialized_where_append_is_not_atomic.py`: Where an append is a seek and then a write, the trail's line is written under a lock.

`scripts/capture_diagnostics.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_trail_append_is_serialized_where_append_is_not_atomic.py -q`.
