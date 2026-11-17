# Tests of `scripts/project_checkpoint.py`

- `tests/test_a_batch_is_named_after_the_batch.py`: A checkpoint batch is named after the whole batch, not after its last event.

`scripts/project_checkpoint.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_batch_is_named_after_the_batch.py -q`.
