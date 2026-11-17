# Tests of `scripts/queue_core.py`

- `tests/test_a_child_that_dies_at_start_does_not_halt_the_worker.py`: A processor child that dies before it says it is up is a failed start, not a failed cleanup.

`scripts/queue_core.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_child_that_dies_at_start_does_not_halt_the_worker.py -q`.
