# Tests of `scripts/queue_core.py`

- `tests/test_one_corrupt_task_does_not_close_the_adopted_queue.py`: One tampered payload is a fact about one task, not about the whole queue.

`scripts/queue_core.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_one_corrupt_task_does_not_close_the_adopted_queue.py -q`.
