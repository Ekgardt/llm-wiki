# Tests of `scripts/installed_memory_repair.py`

- `tests/test_the_queue_is_adopted_before_anything_writes_to_it.py`: The installer adopts Reliability V3 before the runtime sync, not after it.

`scripts/installed_memory_repair.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_queue_is_adopted_before_anything_writes_to_it.py -q`.
