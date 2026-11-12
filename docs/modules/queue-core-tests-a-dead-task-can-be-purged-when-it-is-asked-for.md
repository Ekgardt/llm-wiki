# Tests of `scripts/queue_core.py`

- `tests/test_a_dead_task_can_be_purged_when_it_is_asked_for.py`: Attempts-exhausted work leaves the adopted queue when an operator asks for it.

`scripts/queue_core.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_dead_task_can_be_purged_when_it_is_asked_for.py -q`.
