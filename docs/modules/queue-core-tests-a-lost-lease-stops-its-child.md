# Tests of `scripts/queue_core.py`

- `tests/test_a_lost_lease_stops_its_child.py`: A worker whose lease is lost stops the child running the task instead of waiting it out.

`scripts/queue_core.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_lost_lease_stops_its_child.py -q`.
