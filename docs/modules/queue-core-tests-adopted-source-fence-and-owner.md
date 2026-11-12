# Tests of `scripts/queue_core.py`

- `tests/test_adopted_source_fence_and_owner.py`: The adopted V3 queue carries the source-fence family.

`scripts/queue_core.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_adopted_source_fence_and_owner.py -q`.
