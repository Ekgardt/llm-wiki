# Tests of `scripts/project_checkpoint.py`

- `tests/test_a_retried_checkpoint_is_the_same_batch.py`: A checkpoint whose commit failed is retried as the same batch, even after new events arrive.

`scripts/project_checkpoint.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_retried_checkpoint_is_the_same_batch.py -q`.
