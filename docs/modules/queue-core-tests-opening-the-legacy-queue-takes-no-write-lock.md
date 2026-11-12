# Tests of `scripts/queue_core.py`

- `tests/test_opening_the_legacy_queue_takes_no_write_lock.py`: Opening the legacy queue takes the write lock only when it has work to do.

`scripts/queue_core.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_opening_the_legacy_queue_takes_no_write_lock.py -q`.
