# Tests of `scripts/queue_core.py`

- `tests/test_a_blocked_task_has_a_way_back.py`: A task blocked on a capability can be put back to work by the operator.

`scripts/queue_core.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_blocked_task_has_a_way_back.py -q`.
