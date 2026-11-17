# Tests of `scripts/queue_core.py`

- `tests/test_the_worker_does_not_call_a_broken_database_idle.py`: Only a busy database means "nothing to claim yet"; any other database error is raised.

`scripts/queue_core.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_worker_does_not_call_a_broken_database_idle.py -q`.
