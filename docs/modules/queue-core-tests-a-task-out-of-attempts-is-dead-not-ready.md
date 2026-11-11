# Tests of `scripts/queue_core.py`

- `tests/test_a_task_out_of_attempts_is_dead_not_ready.py`: A task that has spent every attempt must not keep the state of pending work.

`scripts/queue_core.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_task_out_of_attempts_is_dead_not_ready.py -q`.
