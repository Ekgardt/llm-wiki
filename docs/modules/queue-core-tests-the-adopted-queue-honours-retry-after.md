# Tests of `scripts/queue_core.py`

- `tests/test_the_adopted_queue_honours_retry_after.py`: A failure that says when to come back is obeyed by the adopted queue too.

`scripts/queue_core.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_adopted_queue_honours_retry_after.py -q`.
